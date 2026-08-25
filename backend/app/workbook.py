"""Stage 6 of the pipeline: write the row into the client's own workbook.

The workbook does not change shape. Its four registers keep their columns, its
Tax Payable sheet keeps its formulas, and a posted row is written in the same
idiom as the rows already there - including the sheet's own formula patterns,
so a person opening the file cannot tell which rows a human typed.

Two things this module is careful about:

1. It never touches the source file. On first run it copies the master workbook
   into data/workbook/ and appends only to that copy.
2. When a register grows past its totals row, the totals row moves down. Every
   Tax Payable formula that pointed at the old position is repointed, because
   openpyxl does not adjust formulas when rows are inserted.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from decimal import ROUND_HALF_UP, Decimal
from threading import RLock
from typing import Any, Callable

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from . import period as periods
from .config import IRA_INNOVATIONS, WORKBOOK_DIR, source_workbook
from .models import DocumentType, GstTreatment, SupplyType, TaxPayableSummary

# Serialises workbook access. Excel files are a single mutable resource and the
# API can be hit concurrently.
_LOCK = RLock()

TWO_PLACES = Decimal("0.01")


class WorkbookError(RuntimeError):
    pass


@dataclass(frozen=True)
class SheetSpec:
    """Where the data lives on one register sheet."""

    name: str
    header_row: int
    first_data_row: int
    # Column that carries the sheet's SUM() formula, used to locate the totals row.
    totals_probe_column: str
    # Columns that get a SUM() in the totals row.
    total_columns: tuple[str, ...]
    # Human-facing column map: letter -> header label.
    columns: dict[str, str]


SPECS: dict[DocumentType, SheetSpec] = {
    DocumentType.SALES: SheetSpec(
        name="GSTR-1",
        header_row=7,
        first_data_row=8,
        totals_probe_column="J",
        total_columns=("G", "H", "J", "K", "L", "M", "N"),
        columns={
            "A": "Sl.No", "B": "Date", "C": "Invoice no", "D": "GSTIN", "E": "Party Name",
            "F": "HSN/SAC", "G": "No of Bags", "H": "Bag Rate", "I": "Rate",
            "J": "Taxable Value", "K": "IGST", "L": "CGST", "M": "SGST", "N": "Invoice Amount",
        },
    ),
    DocumentType.PURCHASE: SheetSpec(
        name="GSTR-2B",
        header_row=6,
        first_data_row=7,
        totals_probe_column="G",
        total_columns=("G", "H", "I", "J", "K", "L"),
        columns={
            "A": "Filing Period", "B": "Invoice Date", "C": "Invoice Number",
            "D": "Supplier GSTIN", "E": "Supplier Name", "F": "Supply Attract Reverse Charge",
            "G": "Taxable Amount", "H": "IGST Amount", "I": "CGST Amount",
            "J": "SGST Amount", "K": "Cess Amount", "L": "Total",
        },
    ),
    DocumentType.CREDIT_NOTE: SheetSpec(
        name="Credit Note",
        header_row=5,
        first_data_row=6,
        totals_probe_column="K",
        total_columns=("K", "L", "M", "N", "O", "P"),
        columns={
            "A": "S.No", "B": "GSTIN of supplier", "C": "Trade/Legal name", "D": "Note number",
            "E": "Note type", "F": "Note Supply type", "G": "Note date", "H": "Place of supply",
            "I": "Supply Attract Reverse Charge", "J": "Rate(%)", "K": "Taxable Value (Rs)",
            "L": "Integrated Tax(Rs)", "M": "Central Tax(Rs)", "N": "State/UT Tax(Rs)",
            "O": "Cess(Rs)", "P": "Total",
        },
    ),
    DocumentType.RCM: SheetSpec(
        name="RCM",
        header_row=7,
        first_data_row=8,
        totals_probe_column="H",
        total_columns=("H", "I", "J", "K", "L", "M"),
        columns={
            "A": "Filing Period", "B": "Invoice Date", "C": "Invoice Number",
            "D": "Supplier GSTIN", "E": "Supplier Name", "F": "Supply Attract Reverse Charge",
            "G": "Tax Rate", "H": "Taxable Amount", "I": "IGST Amount", "J": "CGST Amount",
            "K": "SGST Amount", "L": "Cess", "M": "Total",
        },
    ),
}

TAX_PAYABLE = "Tax Payable"


# --------------------------------------------------------------------------- #
# Workbook lifecycle
# --------------------------------------------------------------------------- #

def workbook_path(period: str) -> Path:
    """Where this period's workbook lives. One file per return period."""
    return WORKBOOK_DIR / f"gst-{periods.slug(period)}.xlsx"


def master_period() -> str:
    """The period the master workbook itself covers, read from its own header.

    The workbook states this in GSTR-1!A5 ("Return Period : May-26") and every
    other sheet references that cell, so it is the workbook's own declaration
    rather than anything this application has been told.
    """
    src = source_workbook()
    if not src.exists():
        raise WorkbookError(
            f"Source workbook not found at {src}. Set GST_SOURCE_WORKBOOK in .env to its path."
        )
    wb = load_workbook(src, read_only=True)
    try:
        found = periods.period_from_header(wb["GSTR-1"]["A5"].value)
    finally:
        wb.close()
    if not found:
        raise WorkbookError("The master workbook does not declare a return period in GSTR-1!A5.")
    return found


def ensure_working_copy(period: str) -> Path:
    """Get this period's workbook, creating it from the master if needed.

    The master workbook is the template for every period. Opening a period it
    already covers keeps its rows; opening any other period starts from the
    same structure with the registers empty, because May's purchases are not
    July's.
    """
    if not periods.is_valid(period):
        raise WorkbookError(f"{period!r} is not a return period.")

    target = workbook_path(period)
    if target.exists():
        return target

    src = source_workbook()
    if not src.exists():
        raise WorkbookError(
            f"Source workbook not found at {src}. Set GST_SOURCE_WORKBOOK in .env to its path."
        )
    WORKBOOK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)

    if period != master_period():
        _reset_for_new_period(target, period)
    return target


def _reset_for_new_period(path: Path, period: str) -> None:
    """Turn a copy of the master into an empty workbook for another period."""
    wb = load_workbook(path)

    wb["GSTR-1"]["A5"] = periods.header_text(period)  # other sheets reference this cell

    for doc_type, spec in SPECS.items():
        ws = wb[spec.name]
        for row in _data_rows(ws, spec):
            _clear_row(ws, spec, row, doc_type)
        _rewrite_totals(ws, spec, find_totals_row(ws, spec))

    # Opening credit belongs to this period, not the master's. It is left at
    # zero rather than carried over, because an overstated opening credit
    # understates the tax due - the expensive direction to be wrong in. The
    # Tax Payable screen flags it until someone enters the real figure.
    tp = wb[TAX_PAYABLE]
    for cell in ("E6", "F6", "G6"):
        tp[cell] = 0

    _sync_tax_payable(wb)
    wb.save(path)
    wb.close()
    _invalidate(period)


def opening_credit_is_unset(period: str) -> bool:
    """True when this period's opening ITC has not been entered yet."""
    return _cached(("opening_credit", period), period,
                   lambda: _opening_credit_is_unset(period))


def _opening_credit_is_unset(period: str) -> bool:
    with _LOCK:
        wb = _open(period)
        tp = wb[TAX_PAYABLE]
        values = [_number(tp[cell].value) for cell in ("E6", "F6", "G6")]
        wb.close()
    return all(value == 0 for value in values)


def available_periods() -> list[str]:
    """Every period this application holds a workbook for, newest first."""
    WORKBOOK_DIR.mkdir(parents=True, exist_ok=True)
    found = set()
    for path in WORKBOOK_DIR.glob("gst-*.xlsx"):
        candidate = path.stem[len("gst-"):]
        if periods.is_valid(candidate):
            found.add(candidate)
    try:
        found.add(master_period())
    except WorkbookError:
        pass
    return sorted(found, key=periods.sort_key, reverse=True)


def _open(period: str):
    ensure_working_copy(period)
    return load_workbook(workbook_path(period), data_only=False)


def _save(wb, period: str) -> None:
    wb.save(workbook_path(period))


def reset_working_copy(period: str | None = None) -> None:
    """Discard posted rows: one period, or every period if none is named."""
    with _LOCK:
        targets = [period] if period else available_periods()
        for item in targets:
            path = workbook_path(item)
            if path.exists():
                path.unlink()
            _invalidate(item)


# --------------------------------------------------------------------------- #
# Cell helpers
# --------------------------------------------------------------------------- #

def _cell(ws, column: str, row: int):
    return ws[f"{column}{row}"]


def _is_formula(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _number(value: Any) -> Decimal:
    """A cell's numeric value, or zero for anything that is not a plain number."""
    if isinstance(value, bool) or value is None:
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("₹", "").strip()
        try:
            return Decimal(cleaned)
        except Exception:
            return Decimal("0")
    return Decimal("0")


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def find_totals_row(ws, spec: SheetSpec) -> int:
    """Locate the row carrying the sheet's SUM() formulas.

    Registers grow, so the totals row is never assumed to be where it was last
    time. If it cannot be found (an empty register whose SUM row was deleted),
    fall back to the first row after the data block.
    """
    column = spec.totals_probe_column
    for row in range(spec.first_data_row, ws.max_row + 2):
        value = _cell(ws, column, row).value
        if _is_formula(value) and "SUM(" in value.upper():
            return row
    return max(ws.max_row + 1, spec.first_data_row)


def _data_rows(ws, spec: SheetSpec) -> range:
    return range(spec.first_data_row, find_totals_row(ws, spec))


def _row_is_empty(ws, spec: SheetSpec, row: int) -> bool:
    """A GSTR-1 template row counts as empty even though it carries formulas."""
    identity_columns = {
        "GSTR-1": ("C", "E", "J"),
        "GSTR-2B": ("C", "E", "G"),
        "Credit Note": ("D", "C", "K"),
        "RCM": ("C", "E", "H"),
    }[spec.name]
    for column in identity_columns:
        value = _cell(ws, column, row).value
        if value is None:
            continue
        if _is_formula(value):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return False
    return True


# --------------------------------------------------------------------------- #
# Reading registers
# --------------------------------------------------------------------------- #

# Every read re-opened the workbook and re-parsed it. Loading a register sheet
# is tens of milliseconds of XML, and one visit to the Registers screen does it
# four times over before the Tax position screen does it again - all against a
# file that has not changed between them.
#
# The cache is keyed on the workbook's modification time, so it cannot serve a
# stale answer: any write, by this process or by someone editing the file in
# Excel, moves the timestamp and the next read recomputes. Writes also evict
# their period explicitly, so correctness never depends on filesystem timestamp
# resolution.
#
# Cached values are treated as immutable by every caller; they are serialised
# to JSON, not mutated.
_read_cache: dict[tuple, tuple[int, Any]] = {}


def _stamp(period: str) -> int:
    """The workbook's modification time, or -1 if it does not exist yet."""
    try:
        return workbook_path(period).stat().st_mtime_ns
    except OSError:
        return -1


def _cached(key: tuple, period: str, compute: Callable[[], Any]) -> Any:
    with _LOCK:
        stamp = _stamp(period)
        hit = _read_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        value = compute()
        # Re-stamp after computing: _open() creates the workbook on first use,
        # so the timestamp that matters is the one it has now.
        _read_cache[key] = (_stamp(period), value)
        return value


def _invalidate(period: str) -> None:
    """Drop every cached read for one period, after writing to it."""
    with _LOCK:
        for key in [k for k in _read_cache if k[-1] == period]:
            del _read_cache[key]


def _tax_cell(ws, row: int, column: str, taxable: Decimal, rate: Decimal,
              _seen: frozenset[str] = frozenset()) -> Decimal:
    """Evaluate one tax cell, whether it holds a formula or a typed number.

    A row this application posted carries the sheet's own formula idiom; a row
    somebody typed into Excel carries a plain number. Both are legitimate
    entries, so each cell is evaluated on its own terms. Inferring the whole
    CGST/SGST-vs-IGST split from whether *one* cell happens to be a formula
    reads a hand-typed inter-state sale as IGST plus a CGST/SGST that is not on
    the sheet at all, which overstates output tax on the Tax Payable screen.
    """
    raw = _cell(ws, column, row).value
    if not _is_formula(raw):
        return _q(_number(raw))

    formula = str(raw).upper().replace(" ", "").lstrip("=")

    # The half-rate form is checked first: "J8*I8/2" also contains "J8*I8".
    if formula in (f"J{row}*I{row}/2", f"I{row}*J{row}/2"):
        return _q(taxable * rate / Decimal("2"))
    if formula in (f"I{row}*J{row}", f"J{row}*I{row}"):
        return _q(taxable * rate)

    # "=L8" in the SGST column mirrors the CGST cell beside it. The guard stops
    # a pair of cells that point at each other from recursing forever.
    if formula in {f"{letter}{row}" for letter in "KLM"} and formula not in _seen:
        return _tax_cell(ws, row, formula[0], taxable, rate, _seen | {formula})

    # An unrecognised formula: openpyxl reads formulas, not their results, and
    # guessing at one would be worse than declining to score it.
    return Decimal("0.00")


def _gstr1_amounts(ws, row: int) -> dict[str, Decimal]:
    """Evaluate a GSTR-1 row the way the sheet's own formulas would.

    Taxable value is either typed directly or derived from bags x bag rate.
    IGST, CGST and SGST are each read from their own column, so a row typed by
    hand in Excel reads back as what it actually says rather than as whatever
    this application's formula pattern would have put there.
    """
    taxable_raw = _cell(ws, "J", row).value
    if _is_formula(taxable_raw):
        taxable = _q(_number(_cell(ws, "G", row).value) * _number(_cell(ws, "H", row).value))
    else:
        taxable = _q(_number(taxable_raw))

    rate = _number(_cell(ws, "I", row).value)
    igst = _tax_cell(ws, row, "K", taxable, rate)
    cgst = _tax_cell(ws, row, "L", taxable, rate)
    sgst = _tax_cell(ws, row, "M", taxable, rate)
    return {
        "taxable": taxable, "rate": rate, "igst": igst, "cgst": cgst, "sgst": sgst,
        "total": _q(taxable + igst + cgst + sgst),
    }


def _display(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%d-%b-%Y")
    if _is_formula(value):
        return None
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def read_register(doc_type: DocumentType, period: str) -> list[dict]:
    """Every posted row of one register, formatted for display."""
    return _cached(("register", doc_type.value, period), period,
                   lambda: _read_register(doc_type, period))


def _read_register(doc_type: DocumentType, period: str) -> list[dict]:
    spec = SPECS[doc_type]
    with _LOCK:
        wb = _open(period)
        ws = wb[spec.name]
        rows: list[dict] = []
        for row in _data_rows(ws, spec):
            if _row_is_empty(ws, spec, row):
                continue
            values: dict[str, str | None] = {}
            for column, label in spec.columns.items():
                values[label] = _display(_cell(ws, column, row).value)

            if doc_type is DocumentType.SALES:
                amounts = _gstr1_amounts(ws, row)
                values["Rate"] = f"{amounts['rate'] * 100:.2f}%"
                values["Taxable Value"] = f"{amounts['taxable']:,.2f}"
                values["IGST"] = f"{amounts['igst']:,.2f}"
                values["CGST"] = f"{amounts['cgst']:,.2f}"
                values["SGST"] = f"{amounts['sgst']:,.2f}"
                values["Invoice Amount"] = f"{amounts['total']:,.2f}"
            rows.append({"row": row, "values": values})
        wb.close()
    return rows


def register_columns(doc_type: DocumentType) -> list[str]:
    return list(SPECS[doc_type].columns.values())


def posted_keys(doc_type: DocumentType, period: str) -> list[tuple[str, str | None]]:
    """(invoice number, party GSTIN) already present in a register."""
    return _cached(("posted_keys", doc_type.value, period), period,
                   lambda: _posted_keys(doc_type, period))


def _posted_keys(doc_type: DocumentType, period: str) -> list[tuple[str, str | None]]:
    spec = SPECS[doc_type]
    number_column = {"GSTR-1": "C", "GSTR-2B": "C", "Credit Note": "D", "RCM": "C"}[spec.name]
    gstin_column = {"GSTR-1": "D", "GSTR-2B": "D", "Credit Note": "B", "RCM": "D"}[spec.name]
    with _LOCK:
        wb = _open(period)
        ws = wb[spec.name]
        keys: list[tuple[str, str | None]] = []
        for row in _data_rows(ws, spec):
            if _row_is_empty(ws, spec, row):
                continue
            number = _cell(ws, number_column, row).value
            if number is None or _is_formula(number):
                continue
            gstin = _cell(ws, gstin_column, row).value
            keys.append((str(number), str(gstin) if gstin and not _is_formula(gstin) else None))
        wb.close()
    return keys


# --------------------------------------------------------------------------- #
# Growing a register
# --------------------------------------------------------------------------- #

def _rewrite_totals(ws, spec: SheetSpec, totals_row: int) -> None:
    last_data_row = totals_row - 1
    if last_data_row < spec.first_data_row:
        return
    for column in spec.total_columns:
        _cell(ws, column, totals_row).value = (
            f"=SUM({column}{spec.first_data_row}:{column}{last_data_row})"
        )


def _sync_tax_payable(wb) -> None:
    """Repoint every Tax Payable formula at the current totals rows.

    The Tax Payable sheet reads one cell per register. Those references are the
    only thing that breaks when a register grows, so they are rebuilt from the
    live totals-row positions rather than trusted to survive a row insert.
    """
    if TAX_PAYABLE not in wb.sheetnames:
        return
    tp = wb[TAX_PAYABLE]

    purchases = find_totals_row(wb["GSTR-2B"], SPECS[DocumentType.PURCHASE])
    credit = find_totals_row(wb["Credit Note"], SPECS[DocumentType.CREDIT_NOTE])
    rcm = find_totals_row(wb["RCM"], SPECS[DocumentType.RCM])
    sales = find_totals_row(wb["GSTR-1"], SPECS[DocumentType.SALES])

    # ITC block: purchases, credit note reversal, RCM input.
    tp["E7"] = f"=ROUND('GSTR-2B'!H{purchases},0)"
    tp["F7"] = f"=ROUND('GSTR-2B'!I{purchases},0)"
    tp["G7"] = f"=ROUND('GSTR-2B'!J{purchases},0)"
    tp["E8"] = f"=-'Credit Note'!L{credit}"
    tp["F8"] = f"=-'Credit Note'!M{credit}"
    tp["G8"] = f"=-'Credit Note'!N{credit}"
    tp["E9"] = f"=RCM!I{rcm}"
    tp["F9"] = f"=RCM!J{rcm}"
    tp["G9"] = f"=RCM!K{rcm}"

    # Output tax block. The GSTR-1A addends are preserved as the workbook has them.
    tp["D14"] = f"='GSTR-1'!K{sales}"
    tp["D16"] = f"='GSTR-1'!L{sales}+'GSTR-1A'!J50"
    tp["D18"] = f"='GSTR-1'!M{sales}+'GSTR-1A'!K50"


def _allocate_row(ws, spec: SheetSpec) -> int:
    """Find a free data row, growing the sheet if every existing one is used.

    GSTR-1 ships with 64 pre-formatted rows; those are filled first so the
    sheet's existing formatting and formula pattern are reused. Every register
    can also grow: a new row is inserted immediately above the totals row and
    the SUM ranges are rewritten to include it.
    """
    totals_row = find_totals_row(ws, spec)
    for row in range(spec.first_data_row, totals_row):
        if _row_is_empty(ws, spec, row):
            return row

    ws.insert_rows(totals_row)
    new_row = totals_row
    _rewrite_totals(ws, spec, new_row + 1)
    return new_row


# --------------------------------------------------------------------------- #
# Writing a row
# --------------------------------------------------------------------------- #

_DATE_FORMATS = (
    "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
    "%d/%m/%y", "%d-%b-%y", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y",
)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _ddmmyyyy(value: date | None) -> str | None:
    return value.strftime("%d/%m/%Y") if value else None


def _filing_period_value(ws, spec: SheetSpec, period: str) -> Any:
    """Reuse whatever the sheet already puts in its Filing Period column."""
    for row in _data_rows(ws, spec):
        existing = _cell(ws, "A", row).value
        if isinstance(existing, (datetime, date)):
            return existing
    # No precedent: the workbook uses the start of the financial year, which
    # runs April to March, derived from the period this row belongs to.
    parsed = periods.parse_period(period)
    if parsed is None:
        return None
    year, month = parsed
    return datetime(year if month >= 4 else year - 1, 4, 1)


def _serial_number(ws, spec: SheetSpec, row: int) -> int:
    return row - spec.first_data_row + 1


def _clear_row(ws, spec: SheetSpec, row: int, doc_type: DocumentType) -> None:
    """Empty one data row, restoring the sheet's template where it has one.

    GSTR-1 ships with pre-formatted rows carrying their own formulas, so
    clearing one means putting those formulas back rather than leaving a blank.
    """
    for column in spec.columns:
        _cell(ws, column, row).value = None
    if doc_type is not DocumentType.SALES:
        return
    _cell(ws, "A", row).value = _serial_number(ws, spec, row)
    _cell(ws, "I", row).value = 0.18
    _cell(ws, "K", row).value = 0
    _cell(ws, "L", row).value = f"=J{row}*I{row}/2"
    _cell(ws, "M", row).value = f"=L{row}"
    _cell(ws, "N", row).value = f"=J{row}+K{row}+L{row}+M{row}"


def _template_format(ws, spec: SheetSpec, column: str, skip_row: int) -> str | None:
    """The number format this column already uses, read off a sibling row."""
    for row in range(spec.first_data_row, find_totals_row(ws, spec)):
        if row == skip_row:
            continue
        fmt = _cell(ws, column, row).number_format
        if fmt and fmt != "General":
            return fmt
    return None


def _write(ws, spec: SheetSpec, column: str, row: int, value: Any) -> None:
    """Write a cell without disturbing the sheet's own number format.

    The registers' column widths are tuned to the formats already in them - the
    GSTR-1 rate column is 5.1 characters wide because '0%' fits it, and the
    filing period column is 7.3 wide because it shows 'Apr-26'. Imposing a wider
    format like '0.00%' or 'DD-MM-YYYY' does not widen the column: Excel gives
    up and renders '####' instead of the number. So an existing format is always
    left alone, and a row grown past the template borrows the format its
    neighbours use rather than one hard-coded here.
    """
    cell = _cell(ws, column, row)

    # Read the format before writing: assigning a datetime makes openpyxl stamp
    # its own 'yyyy-mm-dd h:mm:ss' on the cell, which would both mask the fact
    # that the cell had no format of its own and be far too wide for a column
    # sized for 'mmm-yy'.
    had_no_format = cell.number_format in {"General", "", None}
    cell.value = value

    if had_no_format:
        inherited = _template_format(ws, spec, column, skip_row=row)
        if inherited:
            cell.number_format = inherited


def _write_sales(ws, spec: SheetSpec, row: int, t: GstTreatment, meta: dict) -> None:
    _write(ws, spec, "A", row, _serial_number(ws, spec, row))
    _write(ws, spec, "B", row, meta.get("invoice_date_obj"))
    _write(ws, spec, "C", row, meta.get("invoice_number"))
    _write(ws, spec, "D", row, t.counterparty_gstin)
    _write(ws, spec, "E", row, t.counterparty_name)
    _write(ws, spec, "F", row, meta.get("hsn_sac"))
    _write(ws, spec, "G", row, meta.get("quantity"))
    _write(ws, spec, "H", row, meta.get("unit_rate"))
    _write(ws, spec, "I", row, float(t.rate))
    _write(ws, spec, "J", row, float(t.taxable_value))

    # Mirror the sheet's own formula idiom rather than pasting numbers, so the
    # row recalculates in Excel exactly like the rows around it.
    if t.supply_type is SupplyType.INTER_STATE:
        _write(ws, spec, "K", row, f"=I{row}*J{row}")
        _write(ws, spec, "L", row, 0)
        _write(ws, spec, "M", row, f"=L{row}")
    else:
        _write(ws, spec, "K", row, 0)
        _write(ws, spec, "L", row, f"=J{row}*I{row}/2")
        _write(ws, spec, "M", row, f"=L{row}")
    _write(ws, spec, "N", row, f"=J{row}+K{row}+L{row}+M{row}")


def _write_purchase(ws, spec: SheetSpec, row: int, t: GstTreatment, meta: dict) -> None:
    _write(ws, spec, "A", row, _filing_period_value(ws, spec, meta["period"]))
    _write(ws, spec, "B", row, _ddmmyyyy(meta.get("invoice_date_obj")))
    _write(ws, spec, "C", row, meta.get("invoice_number"))
    _write(ws, spec, "D", row, t.counterparty_gstin)
    _write(ws, spec, "E", row, (t.counterparty_name or "").upper() or None)
    _write(ws, spec, "F", row, "No")
    _write(ws, spec, "G", row, float(t.taxable_value))
    _write(ws, spec, "H", row, float(t.igst))
    _write(ws, spec, "I", row, float(t.cgst))
    _write(ws, spec, "J", row, float(t.sgst))
    _write(ws, spec, "K", row, float(t.cess))
    _write(ws, spec, "L", row, float(t.invoice_total))


def _write_credit_note(ws, spec: SheetSpec, row: int, t: GstTreatment, meta: dict) -> None:
    _write(ws, spec, "A", row, _serial_number(ws, spec, row))
    _write(ws, spec, "B", row, t.counterparty_gstin)
    _write(ws, spec, "C", row, t.counterparty_name)
    _write(ws, spec, "D", row, meta.get("invoice_number"))
    _write(ws, spec, "E", row, "Credit Note")
    _write(ws, spec, "F", row, "Regular")
    _write(ws, spec, "G", row, _ddmmyyyy(meta.get("invoice_date_obj")))
    _write(ws, spec, "H", row, t.place_of_supply_name)
    _write(ws, spec, "I", row, "No")
    _write(ws, spec, "J", row, float(t.rate))
    _write(ws, spec, "K", row, float(t.taxable_value))
    _write(ws, spec, "L", row, float(t.igst))
    _write(ws, spec, "M", row, float(t.cgst))
    _write(ws, spec, "N", row, float(t.sgst))
    _write(ws, spec, "O", row, float(t.cess))
    _write(ws, spec, "P", row, float(t.invoice_total))


def _write_rcm(ws, spec: SheetSpec, row: int, t: GstTreatment, meta: dict) -> None:
    _write(ws, spec, "A", row, _filing_period_value(ws, spec, meta["period"]))
    _write(ws, spec, "B", row, _ddmmyyyy(meta.get("invoice_date_obj")))
    _write(ws, spec, "C", row, meta.get("invoice_number"))
    _write(ws, spec, "D", row, t.counterparty_gstin)
    _write(ws, spec, "E", row, (t.counterparty_name or "").upper() or None)
    _write(ws, spec, "F", row, "Yes")
    _write(ws, spec, "G", row, float(t.rate))
    _write(ws, spec, "H", row, float(t.taxable_value))
    _write(ws, spec, "I", row, float(t.igst))
    _write(ws, spec, "J", row, float(t.cgst))
    _write(ws, spec, "K", row, float(t.sgst))
    _write(ws, spec, "L", row, float(t.cess))
    _write(ws, spec, "M", row, float(t.invoice_total))


_WRITERS: dict[DocumentType, Callable] = {
    DocumentType.SALES: _write_sales,
    DocumentType.PURCHASE: _write_purchase,
    DocumentType.CREDIT_NOTE: _write_credit_note,
    DocumentType.RCM: _write_rcm,
}


def post_row(treatment: GstTreatment, meta: dict, period: str) -> tuple[str, int]:
    """Append one finished row to this period's register.

    The period comes from the invoice's own date, so an invoice always lands in
    the return it belongs to - no setting decides that on its behalf.
    """
    spec = SPECS[treatment.document_type]
    with _LOCK:
        wb = _open(period)
        ws = wb[spec.name]
        row = _allocate_row(ws, spec)
        _WRITERS[treatment.document_type](ws, spec, row, treatment, meta)
        _rewrite_totals(ws, spec, find_totals_row(ws, spec))
        _sync_tax_payable(wb)
        _save(wb, period)
        wb.close()
        _invalidate(period)
    return spec.name, row


def unpost_row(sheet: str, row: int, period: str) -> None:
    """Clear a posted row, used when a posting is undone from the registers screen."""
    doc_type = next(dt for dt, spec in SPECS.items() if spec.name == sheet)
    spec = SPECS[doc_type]
    with _LOCK:
        wb = _open(period)
        ws = wb[spec.name]
        _clear_row(ws, spec, row, doc_type)
        _rewrite_totals(ws, spec, find_totals_row(ws, spec))
        _sync_tax_payable(wb)
        _save(wb, period)
        wb.close()
        _invalidate(period)


# --------------------------------------------------------------------------- #
# Tax Payable
# --------------------------------------------------------------------------- #

def _sum_columns(ws, spec: SheetSpec, columns: dict[str, str]) -> dict[str, Decimal]:
    totals = {key: Decimal("0.00") for key in columns}
    for row in _data_rows(ws, spec):
        if _row_is_empty(ws, spec, row):
            continue
        for key, column in columns.items():
            totals[key] += _number(_cell(ws, column, row).value)
    return {key: _q(value) for key, value in totals.items()}


def _floats(values: dict[str, Decimal]) -> dict[str, float]:
    return {key: float(value) for key, value in values.items()}


def tax_payable_summary(period: str) -> TaxPayableSummary:
    """The Tax Payable position for one period, recomputed from its registers."""
    return _cached(("tax_payable", period), period, lambda: _tax_payable_summary(period))


def _tax_payable_summary(period: str) -> TaxPayableSummary:
    """Recompute the Tax Payable position from the registers.

    openpyxl reads formulas, not their cached results, so the figures here are
    recomputed in Python using the same arithmetic the sheet performs. That is
    also what makes the position visible mid-month rather than only after Excel
    recalculates.
    """
    with _LOCK:
        wb = _open(period)

        tp = wb[TAX_PAYABLE]
        carry = {
            "igst": _q(_number(tp["E6"].value)),
            "cgst": _q(_number(tp["F6"].value)),
            "sgst": _q(_number(tp["G6"].value)),
        }

        purchases = _sum_columns(
            wb["GSTR-2B"], SPECS[DocumentType.PURCHASE], {"igst": "H", "cgst": "I", "sgst": "J"}
        )
        credit = _sum_columns(
            wb["Credit Note"], SPECS[DocumentType.CREDIT_NOTE], {"igst": "L", "cgst": "M", "sgst": "N"}
        )
        rcm = _sum_columns(wb["RCM"], SPECS[DocumentType.RCM], {"igst": "I", "cgst": "J", "sgst": "K"})

        sales_spec = SPECS[DocumentType.SALES]
        sales_ws = wb["GSTR-1"]
        output = {"igst": Decimal("0.00"), "cgst": Decimal("0.00"), "sgst": Decimal("0.00")}
        for row in _data_rows(sales_ws, sales_spec):
            if _row_is_empty(sales_ws, sales_spec, row):
                continue
            amounts = _gstr1_amounts(sales_ws, row)
            output["igst"] += amounts["igst"]
            output["cgst"] += amounts["cgst"]
            output["sgst"] += amounts["sgst"]
        output = {key: _q(value) for key, value in output.items()}
        wb.close()

    # The sheet rounds the current-month ITC line to whole rupees.
    purchases_rounded = {k: v.quantize(Decimal("1"), rounding=ROUND_HALF_UP) for k, v in purchases.items()}
    reversal = {key: -value for key, value in credit.items()}

    available = {
        key: (carry[key] + purchases_rounded[key] + reversal[key] + rcm[key]).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        for key in ("igst", "cgst", "sgst")
    }

    # Output tax is settled against the credit available; anything left is cash.
    net = {key: _q(output[key] - available[key]) for key in output}

    return TaxPayableSummary(
        itc_carry_forward=_floats(carry),
        itc_current_purchases=_floats(purchases_rounded),
        credit_note_reversal=_floats(reversal),
        rcm_input=_floats(rcm),
        itc_available=_floats(available),
        output_tax=_floats(output),
        # RCM tax is always paid in cash, never set off against credit.
        net_payable=_floats({k: max(net[k], Decimal("0.00")) for k in net}),
        rcm_cash_payable=_floats(rcm),
        return_period=period,
    )


def workbook_info() -> dict:
    known = available_periods()
    return {
        "company": IRA_INNOVATIONS.name,
        "gstin": IRA_INNOVATIONS.gstin,
        "master_period": master_period(),
        "periods": known,
        "source": str(source_workbook()),
        "workbook_dir": str(WORKBOOK_DIR),
        "sheets": {dt.value: spec.name for dt, spec in SPECS.items()},
    }
