"""Offline fallback reader.

When no Claude credentials are configured the system still has to do something
useful rather than stall. This reader parses a PDF's text layer using the
label-then-value shape that billing software emits, and pulls out as much as it
honestly can: party names, both GSTINs, place of supply, HSN, and the invoice's
own totals row.

What it will not do is guess. Anything it cannot read stays null, and every
document it produces is routed to Quick Review rather than auto-confirmed - an
offline read is never good enough to post unseen. The LLM reader in `llm.py`
remains the intended path; this one keeps the app usable without a key.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path

from ..models import ExtractedInvoice, LineItem
from .pdftext import document_text

GSTIN_PATTERN = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
NUMBER_LINE = re.compile(r"^-?[\d,]+\.?\d*$")
HSN_PATTERN = re.compile(r"^\d{4,8}$")
DATE_PATTERN = re.compile(r"^\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{2,4}$|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")
RCM_PATTERN = re.compile(r"reverse\s*charge\s*[:\-]?\s*(yes|applicable)", re.I)
CREDIT_NOTE_PATTERN = re.compile(r"\bcredit\s*note\b", re.I)
PERCENT_PATTERN = re.compile(r"\b(0?\.25|1\.5|3|5|6|9|12|14|18|28)\s*%")

# Lines that are captions rather than data, so a value lookup skips past them.
_NOISE = {
    "", "-", ":", "%", "amount", "original for recipient", "duplicate for supplier",
    "tax invoice", "invoice", "sr.", "no.", "qty", "rate", "total in words",
}


def _clean(line: str) -> str:
    """Strip the leading ' : ' that label-value PDF layouts leave behind."""
    return line.strip().lstrip(":").strip()


def _to_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _is_noise(line: str) -> bool:
    return _clean(line).casefold() in _NOISE


def _value_after(lines: list[str], label: str, *, within: int = 4, skip: int = 0) -> str | None:
    """The first real value following a label line.

    PDF text layers emit 'Invoice No.' on one line and its value on the next,
    sometimes with a stray ':' or blank line between. `skip` lets a caller step
    over a label that repeats, e.g. the second 'Name' in a customer block.
    """
    target = label.casefold()
    for index, line in enumerate(lines):
        if _clean(line).casefold().rstrip(":").strip() != target:
            continue
        if skip > 0:
            skip -= 1
            continue
        for offset in range(1, within + 1):
            if index + offset >= len(lines):
                break
            candidate = lines[index + offset]
            if _is_noise(candidate):
                continue
            return _clean(candidate)
        return None
    return None


def _index_of(lines: list[str], label: str) -> int | None:
    target = label.casefold()
    for index, line in enumerate(lines):
        if _clean(line).casefold().rstrip(":").strip() == target:
            return index
    return None


def _place_of_supply(lines: list[str]) -> str | None:
    """Place of supply, which this layout splits across a name and a code line.

    'Place of' / 'Supply' / 'Andhra Pradesh **' / '( 28 )' has to come back as
    one string so the state resolver sees both the name and the printed code.
    """
    start = _index_of(lines, "place of")
    if start is None:
        start = _index_of(lines, "place of supply")
        offset = 1
    else:
        offset = 2 if start + 1 < len(lines) and _clean(lines[start + 1]).casefold() == "supply" else 1
    if start is None:
        return None

    collected: list[str] = []
    for line in lines[start + offset: start + offset + 3]:
        text = _clean(line)
        if not text or _is_noise(line):
            continue
        # Stop at the next field label.
        if text.casefold().startswith(("invoice", "due date", "phone", "gstin", "address")):
            break
        collected.append(text)
        if ")" in text:  # the '( 28 )' code closes the value
            break
    joined = " ".join(collected).strip()
    return joined or None


# Units that appear inside a totals row between the quantity and the amounts.
_UNIT_TOKENS = {
    "pkt", "pkts", "pcs", "pc", "nos", "no", "box", "boxes", "kg", "kgs", "gm",
    "ltr", "ltrs", "packets", "packet", "bag", "bags", "roll", "rolls", "set",
    "sets", "dozen", "unit", "units", "bdl", "bundle",
}

# The labelled summary block these invoices print beneath the item table. Far
# more reliable than reading the totals row by position, because each figure is
# named rather than inferred from where it sits.
_SUMMARY_LABELS: dict[str, tuple[str, ...]] = {
    "taxable": ("taxable amount", "taxable value", "sub total", "subtotal"),
    "cgst": ("add : cgst", "add: cgst", "add cgst"),
    "sgst": ("add : sgst", "add: sgst", "add sgst"),
    "igst": ("add : igst", "add: igst", "add igst"),
    "total": ("total amount after tax", "grand total", "invoice total", "net amount"),
}


def _normalise_label(line: str) -> str:
    return " ".join(_clean(line).casefold().split())


def _numeric(text: str) -> Decimal | None:
    cleaned = _clean(text).replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    if not cleaned or not NUMBER_LINE.match(cleaned):
        return None
    return _to_decimal(cleaned)


def _labelled_amount(lines: list[str], names: tuple[str, ...]) -> Decimal | None:
    """The amount printed directly beneath a named label.

    Searched from the bottom up, because the summary block sits at the foot of
    the invoice while the same words also appear as column headers higher up.
    The value must be on the very next non-blank line, which is what stops a
    bare 'CGST' column header from matching - the line after it is 'SGST'.
    """
    for index in range(len(lines) - 1, -1, -1):
        if _normalise_label(lines[index]) not in names:
            continue
        for candidate in lines[index + 1: index + 3]:
            if not _clean(candidate):
                continue
            return _numeric(candidate)
    return None


def _summary_block(lines: list[str]) -> dict[str, Decimal | None]:
    """Read the labelled totals block, if the invoice prints one."""
    found = {key: _labelled_amount(lines, names) for key, names in _SUMMARY_LABELS.items()}
    if found["taxable"] is None or found["total"] is None:
        return {}

    tax = sum((v for v in (found["cgst"], found["sgst"], found["igst"]) if v), Decimal("0"))
    # Only trust the block if its own arithmetic holds together.
    if abs(found["taxable"] + tax - found["total"]) > Decimal("0.05"):
        return {}
    return found


def _totals_row(lines: list[str]) -> dict[str, Decimal | None]:
    """Read the invoice's totals row.

    The row is a run of numbers under a standalone 'Total'. Which number is
    which varies by template, so rather than assume positions, this solves for
    the combination that actually adds up to the grand total: taxable + two
    equal tax figures means CGST + SGST, taxable + one means IGST.
    """
    empty = {"taxable": None, "cgst": None, "sgst": None, "igst": None, "total": None, "quantity": None}

    for index in range(len(lines) - 1, -1, -1):
        if _clean(lines[index]).casefold() != "total":
            continue
        numbers: list[Decimal] = []
        for line in lines[index + 1: index + 12]:
            text = _clean(line)
            if not text:
                continue
            # A unit sits between the quantity and the amounts on some layouts
            # ("Total / 1,100.00 / PKT / 17,796.61 / ..."). Step over it rather
            # than treating it as the end of the row.
            if text.casefold() in _UNIT_TOKENS:
                continue
            if not NUMBER_LINE.match(text):
                break
            value = _to_decimal(text)
            if value is None:
                break
            numbers.append(value)
        if len(numbers) < 2:
            continue  # a 'Total' column header, not the totals row

        grand = numbers[-1]
        body = numbers[:-1]

        # Three parts summing to the grand total: taxable + CGST + SGST.
        for combo in combinations(range(len(body)), 3):
            a, b, c = (body[i] for i in combo)
            if abs(a + b + c - grand) > Decimal("0.05"):
                continue
            taxable, taxes = max(a, b, c), sorted([a, b, c])[:2]
            quantity = next((body[i] for i in range(len(body)) if i not in combo), None)
            if taxes[0] == taxes[1]:
                return {"taxable": taxable, "cgst": taxes[0], "sgst": taxes[1],
                        "igst": None, "total": grand, "quantity": quantity}
            return {"taxable": taxable, "cgst": taxes[0], "sgst": taxes[1],
                    "igst": None, "total": grand, "quantity": quantity}

        # Two parts: taxable + a single integrated tax figure.
        for combo in combinations(range(len(body)), 2):
            a, b = (body[i] for i in combo)
            if abs(a + b - grand) > Decimal("0.05"):
                continue
            taxable, tax = max(a, b), min(a, b)
            quantity = next((body[i] for i in range(len(body)) if i not in combo), None)
            return {"taxable": taxable, "cgst": None, "sgst": None,
                    "igst": tax, "total": grand, "quantity": quantity}

        return {**empty, "total": grand}
    return empty


def _totals(lines: list[str]) -> dict[str, Decimal | None]:
    """The invoice's totals, from the labelled summary block where possible.

    Two independent readings, in order of trust: the named summary block at the
    foot of the invoice, then the totals row of the item table. The row is still
    read either way, because it is the only place the total quantity appears.
    """
    row = _totals_row(lines)
    summary = _summary_block(lines)
    if not summary:
        return row

    return {
        "taxable": summary["taxable"],
        "cgst": summary["cgst"],
        "sgst": summary["sgst"],
        "igst": summary["igst"],
        "total": summary["total"],
        "quantity": row.get("quantity"),
    }


def _hsn(lines: list[str]) -> str | None:
    """The first HSN/SAC-shaped code appearing after the item table header."""
    start = _index_of(lines, "hsn / sac") or _index_of(lines, "hsn/sac") or _index_of(lines, "hsn code") or 0
    for line in lines[start + 1:]:
        text = _clean(line)
        if HSN_PATTERN.match(text) and not DATE_PATTERN.match(text):
            return text
    return None


def _customer_block(lines: list[str]) -> tuple[str | None, str | None, str | None]:
    """Name, address and GSTIN from the customer panel, if the layout has one."""
    start = _index_of(lines, "customer detail")
    if start is None:
        start = _index_of(lines, "bill to") or _index_of(lines, "billed to")
    if start is None:
        return None, None, None

    block = lines[start: start + 24]
    name = _value_after(block, "name")
    address_parts: list[str] = []
    address_at = _index_of(block, "address")
    if address_at is not None:
        for line in block[address_at + 1: address_at + 4]:
            text = _clean(line)
            if not text or text.casefold() in {"phone", "gstin", "state"}:
                break
            address_parts.append(text)

    gstin = None
    gstin_at = _index_of(block, "gstin")
    if gstin_at is not None:
        for line in block[gstin_at + 1: gstin_at + 3]:
            found = GSTIN_PATTERN.search(_clean(line))
            if found:
                gstin = found.group(0)
                break
    return name, (" ".join(address_parts).strip() or None), gstin


def extract(path: Path) -> ExtractedInvoice:
    text = document_text(path)
    lines = text.splitlines()

    gstins = GSTIN_PATTERN.findall(text)
    customer_name, customer_address, customer_gstin = _customer_block(lines)

    # The header GSTIN belongs to whoever issued the document.
    supplier_gstin = next((g for g in gstins if g != customer_gstin), None)
    supplier_name = _value_after(lines, "name")
    if supplier_name and customer_name and supplier_name == customer_name:
        supplier_name = None

    totals = _totals(lines)
    percent = PERCENT_PATTERN.search(text)

    # A per-line 9% + 9% is an 18% document; leave the rate null and let the
    # rules engine derive it from the amounts rather than guess here.
    rate = None
    if percent and totals["igst"] is not None:
        rate = float(percent.group(1))

    missing = [
        name for name, value in (
            ("taxable value", totals["taxable"]),
            ("party name", customer_name or supplier_name),
            ("place of supply", _place_of_supply(lines)),
        ) if not value
    ]
    note = "Read from the PDF text layer without the LLM extractor (no Claude credentials configured)."
    if missing:
        note += " Could not find: " + ", ".join(missing) + "."
    if not text.strip():
        note = "This file has no text layer - it is probably a scan. Configure Claude credentials to read it."

    return ExtractedInvoice(
        document_type=None,
        invoice_number=_value_after(lines, "invoice no.") or _value_after(lines, "invoice number"),
        invoice_date=_value_after(lines, "invoice date") or _value_after(lines, "date"),
        supplier_name=supplier_name,
        supplier_gstin=supplier_gstin,
        supplier_address=None,
        recipient_name=customer_name,
        recipient_gstin=customer_gstin,
        recipient_address=customer_address,
        place_of_supply=_place_of_supply(lines),
        reverse_charge=bool(RCM_PATTERN.search(text)),
        is_credit_note=bool(CREDIT_NOTE_PATTERN.search(text)),
        hsn_sac=_hsn(lines),
        line_items=[],
        taxable_value=float(totals["taxable"]) if totals["taxable"] is not None else None,
        gst_rate_percent=rate,
        cgst_amount=float(totals["cgst"]) if totals["cgst"] is not None else None,
        sgst_amount=float(totals["sgst"]) if totals["sgst"] is not None else None,
        igst_amount=float(totals["igst"]) if totals["igst"] is not None else None,
        cess_amount=None,
        total_amount=float(totals["total"]) if totals["total"] is not None else None,
        quantity=float(totals["quantity"]) if totals["quantity"] is not None else None,
        notes=note,
    )
