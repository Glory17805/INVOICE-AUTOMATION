"""Reading an invoice's item table, one row at a time.

The reader this joins treated an invoice's figures as a single taxable value
and a single tax figure. That is right for a bill with one rate on it and
quietly wrong for a bill with two: rice at 5% and pipes at 18% came back as
one row taxed at 13.67%, a rate that does not exist, and every check in the
system passed it.

The fix is to stop summarising and start reading the table. That needs the
PDF's text with its horizontal positions preserved - `pdftext.layout_text` -
because a table is a spatial object. In that form a row is one line and its
columns line up under their headings, so a value can be assigned to a column
by asking which heading it sits beneath.

Assignment is by span overlap rather than by counting fields left to right.
Counting breaks on the first row with an empty cell, and empty cells are
common: a line with no HSN, a service line with no quantity. Overlap does not
care, because it asks where a value is, not how many came before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- #
# Recognising a column by its heading
# --------------------------------------------------------------------------- #

# Heading text -> the role that column plays. Longest match wins, so
# "taxable value" is not read as the bare "value".
ROLES: dict[str, tuple[str, ...]] = {
    "serial": ("sr no", "sr", "s no", "sl no", "sl", "no", "#", "item no"),
    "description": (
        "name of product / service", "name of product", "description of goods",
        "description of services", "particulars", "description", "item name",
        "product name", "goods", "item", "product", "service", "name",
    ),
    "hsn": ("hsn / sac", "hsn/sac", "hsn sac", "hsn code", "sac code", "hsn", "sac"),
    "quantity": ("quantity", "qty"),
    "unit": ("unit", "uom", "u.q.c", "uqc"),
    "unit_rate": ("unit price", "rate per", "price", "rate"),
    "taxable": (
        "taxable value", "taxable amount", "net amount", "net value",
        "assessable value", "amount", "value",
    ),
    "discount": ("discount", "disc"),
    "total": ("total amount", "total"),
}

# Headings that qualify the columns beneath them rather than being columns
# themselves: "CGST" spanning a "%" and an "Amount".
_GROUPS: dict[str, tuple[str, ...]] = {
    "cgst": ("cgst",),
    "sgst": ("sgst", "utgst", "sgst/utgst"),
    "igst": ("igst",),
    "cess": ("cess", "compensation cess"),
    "gst": ("gst", "tax", "gst rate"),
}

# Sub-headings that only mean something under a group.
_SUB_ROLES: dict[str, tuple[str, ...]] = {
    "pct": ("%", "rate", "rate %", "%age"),
    "amount": ("amount", "amt", "value"),
}

_TOKEN = re.compile(r"\S+(?: \S+)*")
_NUMBER = re.compile(r"^\(?-?[₹Rs.\s]*[\d,]+\.?\d*\)?%?$")
_INTEGER = re.compile(r"^\d{1,4}$")


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .:-")


def _role_of(text: str, table: dict[str, tuple[str, ...]]) -> str | None:
    cleaned = _norm(text)
    best: tuple[int, str] | None = None
    for role, names in table.items():
        for name in names:
            matches = (cleaned == name
                       or cleaned.startswith(name + " ")
                       or cleaned.endswith(" " + name))
            if matches and (best is None or len(name) > best[0]):
                best = (len(name), role)
    return best[1] if best else None


def _to_decimal(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d.\-]", "", text.replace("(", "-"))
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


# --------------------------------------------------------------------------- #
# Columns
# --------------------------------------------------------------------------- #

@dataclass
class Token:
    text: str
    start: int
    end: int

    @property
    def centre(self) -> float:
        return (self.start + self.end) / 2


@dataclass
class Column:
    role: str          # "hsn", "cgst_pct", "taxable", ...
    heading: str
    start: int
    end: int

    @property
    def centre(self) -> float:
        return (self.start + self.end) / 2

    def overlap(self, token: Token) -> int:
        return max(0, min(self.end, token.end) - max(self.start, token.start))


def _tokens(line: str) -> list[Token]:
    """The line's cells. Two or more spaces separate one cell from the next."""
    return [Token(m.group().strip(), m.start(), m.start() + len(m.group().rstrip()))
            for m in re.finditer(r"\S+(?:[ ]\S+)*", line) if m.group().strip()]


# Headings that mean nothing on their own. "Amount" is a tax column under a
# "CGST" banner and the taxable column without one; "Rate" is the GST
# percentage under a banner and the unit price without one. Only these are
# candidates for being claimed by a group - a heading that names itself fully,
# like "Taxable Value", is never claimed.
_AMBIGUOUS = frozenset({"%", "%age", "amount", "amt", "value", "rate", "rate %"})

# How far a sub-heading may sit from its banner. A banner is centred over its
# sub-columns, so its own half-width plus a column is generous; much wider and
# a "%" starts being claimed by the tax group two columns away.
_GROUP_REACH = 22


def _columns_from(header_lines: list[tuple[int, str]],
                  roles: dict[str, tuple[str, ...]]) -> list[Column]:
    """Turn a header block into a flat list of columns.

    Group headings ("CGST") sit on a line above their sub-headings ("%",
    "Amount"), so each ambiguous sub-heading takes the name of the nearest
    banner strictly above it, giving "cgst_pct" and "cgst_amount".
    """
    groups: list[tuple[str, int, Token]] = []      # role, line, token
    ambiguous: list[tuple[int, Token]] = []        # line, token
    plain: list[Column] = []

    for line_no, line in header_lines:
        for token in _tokens(line):
            group = _role_of(token.text, _GROUPS)
            if group:
                groups.append((group, line_no, token))
                continue
            if _norm(token.text) in _AMBIGUOUS:
                ambiguous.append((line_no, token))
                continue
            role = _role_of(token.text, roles)
            if role:
                plain.append(Column(role, token.text, token.start, token.end))

    for line_no, token in ambiguous:
        above = [g for g in groups
                 if g[1] < line_no and abs(g[2].centre - token.centre) <= _GROUP_REACH]
        if above:
            name, _, _ = min(above, key=lambda g: abs(g[2].centre - token.centre))
            sub = _role_of(token.text, _SUB_ROLES) or "amount"
            plain.append(Column(f"{name}_{sub}", token.text, token.start, token.end))
            continue
        # No banner over it, so it means what it says.
        role = _role_of(token.text, roles)
        if role:
            plain.append(Column(role, token.text, token.start, token.end))

    # A banner with nothing under it is a column in its own right - "IGST"
    # alone above a column of amounts.
    for name, _, token in groups:
        if not any(c.role.startswith(name + "_") for c in plain):
            plain.append(Column(f"{name}_amount", token.text, token.start, token.end))

    return _dedupe(plain)


def _dedupe(columns: list[Column]) -> list[Column]:
    """Collapse headings that stack, like "Sr." over "No.".

    Two columns of the same role whose spans overlap are one column written on
    two lines; the wider span is the one that will catch its values.
    """
    columns.sort(key=lambda c: (c.start, -(c.end - c.start)))
    kept: list[Column] = []
    for column in columns:
        clash = next((k for k in kept
                      if k.role == column.role and k.overlap(Token("", column.start, column.end))), None)
        if clash is None:
            kept.append(column)
        elif (column.end - column.start) > (clash.end - clash.start):
            kept[kept.index(clash)] = column
    kept.sort(key=lambda c: c.start)
    return kept


# --------------------------------------------------------------------------- #
# Finding the table
# --------------------------------------------------------------------------- #

# What a header block must contain before it is believed. Two coincidental
# words are not a table; a description or an HSN column alongside a figure is.
_REQUIRED_ANY = {"description", "hsn"}
_REQUIRED_FIGURE = {"taxable", "unit_rate", "quantity", "total"}

MAX_HEADER_LINES = 3


@dataclass
class Table:
    header_line: int
    body_start: int
    body_end: int
    columns: list[Column]
    items: list[LineItem] = field(default_factory=list)

    def column(self, role: str) -> Column | None:
        return next((c for c in self.columns if c.role == role), None)

    @property
    def roles(self) -> set[str]:
        return {c.role for c in self.columns}


def _header_block(lines: list[str], start: int,
                  roles: dict[str, tuple[str, ...]]) -> tuple[list[Column], int] | None:
    """A header beginning at `start`, if one does."""
    for depth in range(1, MAX_HEADER_LINES + 1):
        block = [(i, lines[i]) for i in range(start, min(start + depth, len(lines)))]
        columns = _columns_from(block, roles)
        # Named apart from `roles`, which is the heading vocabulary: these are
        # the roles this particular header turned out to have.
        found = {c.role for c in columns}
        if found & _REQUIRED_ANY and found & _REQUIRED_FIGURE and len(columns) >= 3:
            # Take the following line too when it holds only sub-headings.
            nxt = start + depth
            if nxt < len(lines):
                wider = _columns_from([*block, (nxt, lines[nxt])], roles)
                if len(wider) > len(columns):
                    return wider, nxt + 1
            return columns, start + depth
    return None


def _is_totals_row(tokens: list[Token], columns: list[Column]) -> bool:
    """The row that closes the table, which is a total rather than an item."""
    label = next((t for t in tokens if not _NUMBER.match(t.text)), None)
    return label is not None and _norm(label.text) in {"total", "grand total", "sub total", "subtotal"}


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

@dataclass
class LineItem:
    description: str | None = None
    hsn: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_rate: Decimal | None = None
    taxable_value: Decimal | None = None
    discount: Decimal | None = None
    gst_rate_percent: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    cess: Decimal | None = None
    total: Decimal | None = None

    @property
    def total_tax(self) -> Decimal:
        return sum((v for v in (self.cgst, self.sgst, self.igst) if v is not None),
                   Decimal("0"))

    def as_dict(self) -> dict:
        def out(value):
            return str(value) if isinstance(value, Decimal) else value
        return {k: out(v) for k, v in self.__dict__.items()}


def _assign(tokens: list[Token], columns: list[Column]) -> dict[str, str]:
    """Put each cell of a row under the column it sits beneath."""
    cells: dict[str, str] = {}
    for token in tokens:
        best = max(columns, key=lambda c: (c.overlap(token), -abs(c.centre - token.centre)))
        if best.overlap(token) == 0 and abs(best.centre - token.centre) > 12:
            continue  # nothing above it; not part of the table
        cells[best.role] = (cells.get(best.role, "") + " " + token.text).strip()
    return cells


def _derive_rate(item: LineItem, cells: dict[str, str]) -> Decimal | None:
    """The item's GST rate, from whichever column the invoice prints.

    A CGST column shows half the rate, because the other half is the SGST
    column beside it. Doubling it is what makes 9% + 9% read as the 18%
    document it is.
    """
    for role, multiplier in (("gst_pct", 1), ("igst_pct", 1),
                             ("cgst_pct", 2), ("sgst_pct", 2)):
        raw = cells.get(role)
        value = _to_decimal(raw) if raw else None
        if value is not None and value > 0:
            return value * multiplier

    # No rate column: derive it from the amounts the row does print.
    if item.taxable_value and item.taxable_value > 0 and item.total_tax > 0:
        return (item.total_tax / item.taxable_value * 100).quantize(Decimal("0.01"))
    return None


def _row_to_item(cells: dict[str, str]) -> LineItem | None:
    item = LineItem(
        description=cells.get("description") or None,
        hsn=(cells.get("hsn") or "").strip() or None,
        unit=cells.get("unit") or None,
    )
    for role, attr in (("quantity", "quantity"), ("unit_rate", "unit_rate"),
                       ("taxable", "taxable_value"), ("discount", "discount"),
                       ("cgst_amount", "cgst"), ("sgst_amount", "sgst"),
                       ("igst_amount", "igst"), ("cess_amount", "cess"),
                       ("total", "total")):
        raw = cells.get(role)
        if raw:
            setattr(item, attr, _to_decimal(raw))

    item.gst_rate_percent = _derive_rate(item, cells)

    # A row has to say what it is and what it cost. Anything less is a stray
    # line caught between the header and the first item.
    if not (item.description or item.hsn):
        return None
    if item.taxable_value is None and item.total is None and item.unit_rate is None:
        return None
    return item


def _is_continuation(cells: dict[str, str]) -> bool:
    """A wrapped description, which belongs to the item above it."""
    return set(cells) == {"description"}


def find_table(text: str, roles: dict[str, tuple[str, ...]] | None = None) -> Table | None:
    """The item table in a layout-preserved rendering of an invoice."""
    roles = roles or ROLES
    lines = text.splitlines()
    for index in range(len(lines)):
        if not lines[index].strip():
            continue
        found = _header_block(lines, index, roles)
        if not found:
            continue
        columns, body_start = found

        items: list[LineItem] = []
        blanks = 0
        row = body_start
        while row < len(lines):
            line = lines[row]
            if not line.strip():
                blanks += 1
                if blanks >= 6 and items:
                    break
                row += 1
                continue
            blanks = 0

            tokens = _tokens(line)
            if _is_totals_row(tokens, columns):
                break

            cells = _assign(tokens, columns)
            if _is_continuation(cells) and items:
                items[-1].description = f"{items[-1].description} {cells['description']}".strip()
                row += 1
                continue

            item = _row_to_item(cells)
            if item:
                items.append(item)
            row += 1

        if items:
            return Table(index, body_start, row, columns, items)
    return None


def parse(text: str, roles: dict[str, tuple[str, ...]] | None = None) -> list[LineItem]:
    """Every item on the invoice, or an empty list if no table was found."""
    table = find_table(text, roles)
    return table.items if table else []


def distinct_rates(items: list[LineItem]) -> list[Decimal]:
    """The GST rates the invoice carries, in order of first appearance."""
    seen: list[Decimal] = []
    for item in items:
        rate = item.gst_rate_percent
        if rate is not None and rate > 0 and rate not in seen:
            seen.append(rate)
    return seen


def distinct_hsn(items: list[LineItem]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item.hsn and item.hsn not in seen:
            seen.append(item.hsn)
    return seen
