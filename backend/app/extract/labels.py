"""Finding a labelled value on an invoice, whatever the invoice calls it.

The reader this replaces could only follow one convention: a label alone on
one line, its value alone on the next. That is what one vendor's software
emits, and an invoice written any other way returned almost nothing - a bill
printing `Invoice No: INV-118    Date: 12-Jul-2026` on a single line yielded
no invoice number and no date at all.

Two ideas fix that.

The first is an alias vocabulary. Every field names the phrasings that mean
it, so "Invoice No.", "Bill No" and "Tax Invoice No" all resolve to the same
internal field, and adding a vendor's wording is a one-line change here rather
than a new branch in the parser.

The second is scanning for every label on the page at once, before asking for
any particular one. That is what makes same-line layouts work: the value of a
label runs until the next label begins, so on a line holding two pairs each
value ends where its neighbour starts. Knowing only the label you want cannot
tell you where its value stops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

# --------------------------------------------------------------------------- #
# The vocabulary
# --------------------------------------------------------------------------- #

# Canonical field -> the phrasings that mean it. Add a vendor's wording here.
#
# Aliases are matched longest-first, so "Invoice Number" wins over "Invoice
# No" on a line carrying the longer form, and no alias may be a bare word that
# also appears in prose ("Total" alone is a column header as often as it is a
# label, and is handled by the amounts reader instead).
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "invoice_number": (
        "invoice number", "invoice no", "invoice #", "invoice num",
        "tax invoice no", "tax invoice number", "bill number", "bill no",
        "inv no", "inv #", "document number", "document no", "voucher no",
    ),
    "invoice_date": (
        "invoice date", "date of invoice", "bill date", "dated", "inv date",
        "document date", "date of issue", "issue date",
    ),
    "due_date": ("due date", "payment due", "due on"),
    "supplier_name": (
        "supplier name", "seller name", "vendor name", "sold by",
        "supplier", "seller", "vendor",
    ),
    "customer_name": (
        "customer name", "buyer name", "billed to", "bill to", "billing name",
        "party name", "customer detail", "customer details", "buyer", "consignee",
        "ship to", "shipped to",
    ),
    # A bare "Name", which appears in the supplier panel, the customer panel
    # and the bank details alike. Meaningless on its own, which is why it is
    # its own field rather than an alias of either party: the reader asks for
    # it only inside a block it has already located, where position settles
    # whose name it is.
    "entity_name": ("name",),
    "gstin": (
        "gstin / uin", "gstin/uin", "gst no", "gst number", "gstin no",
        "gstin", "gst identification number", "uin",
    ),
    "supplier_gstin": ("supplier gstin", "seller gstin", "vendor gstin", "our gstin"),
    "customer_gstin": (
        "customer gstin", "buyer gstin", "party gstin", "recipient gstin",
        "gstin of recipient", "billed to gstin",
    ),
    "place_of_supply": (
        "place of supply", "place of", "pos", "supply state", "state of supply",
    ),
    "state": ("state name", "state code", "state"),
    "hsn": ("hsn / sac", "hsn/sac", "hsn code", "sac code", "hsn sac", "hsn", "sac"),
    "address": ("address", "billing address", "shipping address"),
    "phone": ("phone", "mobile", "contact no", "telephone"),
    "reverse_charge": (
        "whether tax payable on reverse charge basis",
        "tax payable on reverse charge", "reverse charge basis",
        "reverse charge applicable", "reverse charge", "rcm",
    ),
    # Amounts. The amounts reader owns their arithmetic; these are how they
    # are named on the page.
    "taxable_value": (
        "taxable amount", "taxable value", "total taxable value", "sub total",
        "subtotal", "net amount before tax", "amount before tax", "basic amount",
    ),
    "cgst": ("add : cgst", "add: cgst", "add cgst", "cgst amount", "cgst"),
    "sgst": ("add : sgst", "add: sgst", "add sgst", "sgst amount",
             "sgst/utgst", "utgst", "sgst"),
    "igst": ("add : igst", "add: igst", "add igst", "igst amount", "igst"),
    "cess": ("add : cess", "add: cess", "add cess", "cess amount",
             "compensation cess", "cess"),
    "discount": ("less : discount", "less: discount", "less discount",
                 "discount amount", "discount"),
    "freight": ("freight charges", "freight & forwarding", "freight", "shipping charges",
                "transport charges", "courier charges", "delivery charges"),
    "packing": ("packing & forwarding", "packing charges", "packing and forwarding",
                "packaging charges", "packing"),
    "other_charges": ("other charges", "misc charges", "miscellaneous charges",
                      "insurance charges", "loading charges", "installation charges"),
    "round_off": ("round off", "rounded off", "rounding off", "round-off", "roundoff"),
    "total": (
        "total amount after tax", "grand total", "invoice total", "net amount",
        "total invoice value", "amount payable", "total payable", "bill amount",
        "net payable", "total amount",
    ),
    "amount_in_words": ("total in words", "amount in words", "rupees in words",
                        "in words"),
    "quantity": ("total quantity", "total qty", "quantity", "qty"),
}

# Labels whose value is a whole block rather than a phrase - asking for the
# "value after" one of these gives the next line, which is rarely what is
# wanted, so the field readers treat them as section markers.
SECTION_LABELS = frozenset({"customer_name", "supplier_name", "address"})

# Lines that are page furniture. A next-line lookup steps over them.
NOISE = frozenset({
    "", "-", ":", "%", "|", "amount", "original for recipient",
    "duplicate for supplier", "triplicate for supplier", "original", "duplicate",
    "tax invoice", "invoice", "sr.", "sr. no.", "s.no", "no.", "qty", "rate",
    "total in words", "e. & o.e.", "e&oe", "continued", "page",
})


# Characters PDFs are full of that each mean the ordinary one as far as
# matching a label goes. Built from code points because a non-breaking
# space and an en dash are invisible or near-indistinguishable in source,
# and a reader cannot tell them from a space and a hyphen.
NBSP = chr(0xA0)
EN, EM = chr(0x2013), chr(0x2014)
TRIM_CHARS = " :.-" + EN + EM


def normalise(text: str) -> str:
    """Compare labels without punctuation or spacing getting in the way."""
    # PDFs are full of non-breaking spaces and typographic dashes; both mean
    # the ordinary character as far as matching a label goes.
    flattened = text.casefold().replace(NBSP, " ")
    return " ".join(flattened.split()).strip(TRIM_CHARS)


def is_noise(line: str) -> bool:
    return normalise(line) in NOISE


# A money value and nothing else. Anchored at both ends on purpose: a cell
# holding "10,683.00 12,605.94" is two columns that layout mode ran together,
# and reading the first of them as the answer would be a guess.
_AMOUNT = re.compile(r"^\(?-?(?:rs\.?|inr|₹)?\s*[\d,]+(?:\.\d{1,2})?\)?%?$", re.I)
_CURRENCY = re.compile(r"^[(\-\s]*(?:rs\.?|inr|₹)\s*", re.I)


def to_amount(text: str) -> Decimal | None:
    """The number a cell holds, or None if it does not hold exactly one."""
    cleaned = text.strip()
    if not _AMOUNT.match(cleaned):
        return None
    # Both ways an invoice writes a negative: a leading minus, or brackets in
    # the accounting style. Stripping punctuation to get the digits loses the
    # sign, so it has to be read before that happens.
    negative = cleaned.startswith("-") or (cleaned.startswith("(") and cleaned.endswith(")"))
    # Drop the currency prefix before hunting for digits. "Rs. 900" otherwise
    # keeps the abbreviation's full stop, and ".900" reads as nine-tenths of a
    # rupee rather than nine hundred.
    cleaned = _CURRENCY.sub("", cleaned)
    digits = re.sub(r"[^\d.]", "", cleaned)
    if not digits or digits.count(".") > 1:
        return None
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    return -value if negative else value


# --------------------------------------------------------------------------- #
# Scanning a page for every label on it
# --------------------------------------------------------------------------- #

Vocabulary = dict[str, tuple[str, ...]]


def _key(vocabulary: Vocabulary) -> tuple:
    """A hashable form of a vocabulary, so compiled patterns can be cached."""
    return tuple(sorted((field, tuple(aliases)) for field, aliases in vocabulary.items()))


@lru_cache(maxsize=16)
def _compiled(key: tuple) -> tuple[re.Pattern[str], dict[str, str]]:
    """One pattern matching any alias of any field, longest first.

    Longest-first matters: with "invoice no" ahead of "invoice number" in the
    alternation, the shorter one wins on a line carrying the longer form and
    the value would begin with the leftover "mber".

    Cached because building it is not free and a vendor template produces one
    vocabulary per vendor, not one per document.
    """
    pairs = [(alias, field) for field, aliases in key for alias in aliases]
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    body = "|".join(re.escape(alias).replace(r"\ ", r"[\s\.]*") for alias, _ in pairs)
    # Not \b on either side: several aliases end in punctuation ("invoice #"),
    # where \b asserts the wrong thing. A lookbehind for a non-letter is what
    # actually stops "pos" matching inside "position".
    pattern = re.compile(rf"(?<![A-Za-z0-9])({body})(?![A-Za-z0-9])", re.I)
    lookup = {normalise(alias): field for alias, field in pairs}
    return pattern, lookup

# What may sit between a label and its value: a colon, a dash, a full stop
# left behind by an abbreviated label ("Invoice No." matches the alias
# "invoice no" and leaves the stop), or just space.
#
# A dash is only a separator when a number does not follow it. "Round Off
# -0.50" is a negative amount, and stripping that minus as punctuation turned
# a deduction into an addition - a one-rupee error that stopped the invoice
# balancing at all.
_SEPARATOR = re.compile(r"^(?:[\s:|>.]|[-" + EN + EM + r"](?![\d.]))*")

# A same-line remainder with no letters or digits in it is not a value - it is
# the tail of the label, or the dash a vendor prints to mean "none". Treating
# it as a value is worse than finding nothing, because it stops the next-line
# lookup that would have found the real one.
_HAS_CONTENT = re.compile(r"[A-Za-z0-9]")


@dataclass(frozen=True)
class Hit:
    """One label found on the page."""

    field: str
    line: int
    start: int   # index in the line where the label begins
    end: int     # index just past the label
    alias: str

    @property
    def is_section(self) -> bool:
        return self.field in SECTION_LABELS


def scan(lines: list[str], vocabulary: Vocabulary | None = None) -> list[Hit]:
    """Every label on the page, in reading order."""
    pattern, lookup = _compiled(_key(vocabulary or FIELD_ALIASES))
    hits: list[Hit] = []
    for index, line in enumerate(lines):
        for match in pattern.finditer(line):
            field = lookup.get(normalise(match.group(1)))
            if field:
                hits.append(Hit(field, index, match.start(), match.end(), match.group(1)))
    return hits


class Page:
    """A document's lines, with every label on them already located.

    Field readers ask this for values rather than walking the lines
    themselves, so the same-line and next-line conventions are handled once
    here instead of once per field.
    """

    def __init__(self, text: str, vocabulary: Vocabulary | None = None) -> None:
        self.text = text
        self.lines = text.splitlines()
        self.hits = scan(self.lines, vocabulary)
        self._by_field: dict[str, list[Hit]] = {}
        for hit in self.hits:
            self._by_field.setdefault(hit.field, []).append(hit)

    # -- locating ---------------------------------------------------------- #

    def hits_for(self, field: str) -> list[Hit]:
        return self._by_field.get(field, [])

    def has(self, field: str) -> bool:
        return field in self._by_field

    def _next_label_on_line(self, hit: Hit) -> int | None:
        """Where the following label on the same line starts, if there is one.

        This is what makes `Invoice No: X    Date: Y` work: X ends where the
        "Date" label begins.
        """
        starts = [h.start for h in self.hits
                  if h.line == hit.line and h.start >= hit.end]
        return min(starts) if starts else None

    # -- reading ----------------------------------------------------------- #

    def _same_line_value(self, hit: Hit) -> str:
        line = self.lines[hit.line]
        stop = self._next_label_on_line(hit)
        segment = line[hit.end: stop if stop is not None else len(line)]
        value = _SEPARATOR.sub("", segment).strip()
        return value if _HAS_CONTENT.search(value) else ""

    def _next_line_value(self, hit: Hit, *, within: int) -> str:
        """The first line under the label that carries something.

        Stops at another label so a missing value cannot borrow the next
        field's - a blank "GSTIN" followed by "Place of Supply / Karnataka"
        must read as blank, not as Karnataka.
        """
        for offset in range(1, within + 1):
            index = hit.line + offset
            if index >= len(self.lines):
                return ""
            if any(h.line == index and h.start == 0 for h in self.hits):
                return ""
            candidate = self.lines[index]
            if is_noise(candidate) or self._is_all_labels(index):
                continue
            return candidate.strip().lstrip(":").strip()
        return ""

    def _is_all_labels(self, index: int) -> bool:
        """Whether a line is nothing but headings.

        The row of column headings under a table's banner - "% Amount %
        Amount" - is not the value of anything above it.
        """
        line = self.lines[index]
        if not line.strip():
            return False
        covered = sum(h.end - h.start for h in self.hits if h.line == index)
        return covered > 0 and covered >= len(line.strip()) * 0.6

    def value(self, field: str, *, within: int = 4, occurrence: int = 0) -> str | None:
        """The value of a field, from whichever convention the page uses.

        Same-line first, because a label with something after it on its own
        line is unambiguous. Only when there is nothing after it does the
        next-line convention apply.
        """
        hits = self.hits_for(field)
        if occurrence >= len(hits):
            return None
        for hit in hits[occurrence:]:
            found = self._same_line_value(hit) or self._next_line_value(hit, within=within)
            if found:
                return found
        return None

    def value_between(self, field: str, start_line: int, end_line: int,
                      *, within: int = 4) -> str | None:
        """The value of a field, but only where the label falls in a range.

        Used for the fields an invoice prints twice - "Name" and "GSTIN"
        appear in both the supplier block and the customer block, and which
        one is meant is decided by where it sits on the page.
        """
        for hit in self.hits_for(field):
            if start_line <= hit.line < end_line:
                found = self._same_line_value(hit) or self._next_line_value(hit, within=within)
                if found:
                    return found
        return None

    def amount(self, field: str, *, within: int = 3) -> Decimal | None:
        """A money value, searched from the foot of the page upwards.

        Two things make this different from `value`. It insists on a number,
        and it reads bottom-up.

        Both come from the same problem: the words that name an amount in the
        summary block also name a column in the item table above it, and the
        table comes first. Asking for "Taxable Value" top-down finds the
        column heading and returns whatever sits under it - on a real invoice
        that was the row of sub-headings, "% Amount". The summary block is at
        the bottom of the page, so that is where to start looking, and a
        heading that yields no number is passed over rather than believed.
        """
        for hit in reversed(self.hits_for(field)):
            raw = self._same_line_value(hit) or self._next_line_value(hit, within=within)
            found = to_amount(raw) if raw else None
            if found is not None:
                return found
        return None

    def line_of(self, field: str, *, occurrence: int = 0) -> int | None:
        hits = self.hits_for(field)
        return hits[occurrence].line if occurrence < len(hits) else None
