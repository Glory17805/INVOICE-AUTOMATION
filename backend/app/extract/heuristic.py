"""The offline reader: an invoice to structured fields, without an LLM.

This module is now an orchestrator rather than a parser. The work is split
into parts that can each be tested on their own and replaced without
disturbing the rest:

    pdftext     text for the document, from its text layer or by OCR
    templates   per-vendor overrides, where the general rules need help
    labels      finding a labelled value, whatever the invoice calls it
    lineitems   reading the item table row by row
    amounts     what the invoice adds up to, charges and round-off included
    confidence  how much of the above to trust, field by field

The order matters. Text first, because everything works on text. The template
next, because it changes the vocabulary the rest use. Then the table, because
its rows are the strongest evidence there is - a rate read off an item row is
a fact, where a rate derived by dividing one total by another is an inference.
The summary block fills what the table does not print, and the confidence
report says which is which.

What it will not do is guess. A field it cannot read stays null, and nothing
it produces is posted unseen.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from ..gst.states import resolve_place_of_supply
from ..models import ExtractedInvoice
from ..models import LineItem as ModelLineItem
from . import amounts as amounts_reader
from . import lineitems, ocr, pdftext, templates
from .confidence import Report, assess
from .labels import FIELD_ALIASES, Page, is_noise

GSTIN_PATTERN = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
HSN_PATTERN = re.compile(r"^\d{4,8}$")
CREDIT_NOTE_PATTERN = re.compile(r"\bcredit\s*note\b", re.I)

# Reverse charge is a yes/no answer that follows the words "reverse charge",
# but how far behind and worded how varies. Requiring the answer immediately
# after the phrase missed the wording the GST portal itself uses - "Whether
# tax payable on reverse charge basis" with "Yes" on the next line - and filed
# those bills on the normal register instead of RCM.
_RCM_LABEL = re.compile(r"reverse\s*charge\b", re.I)
_RCM_ANSWER = re.compile(r"\b(not\s+applicable|n\s*/\s*a|nil|yes|no|applicable)\b", re.I)
_RCM_MEANS_YES = {"yes", "applicable"}
_RCM_WINDOW = 60


def _reverse_charge(text: str) -> bool:
    """Whether the document says reverse charge applies.

    The first answer after the phrase wins, so an explicit "No" is honoured
    rather than skipped over in search of a "Yes" further down the page. A
    document that names reverse charge but answers nothing reads as false:
    silently claiming RCM would move the tax liability to the wrong party.
    """
    for label in _RCM_LABEL.finditer(text):
        answer = _RCM_ANSWER.search(text[label.end(): label.end() + _RCM_WINDOW])
        if answer and " ".join(answer.group(1).casefold().split()) in _RCM_MEANS_YES:
            return True
    return False


# --------------------------------------------------------------------------- #
# Parties
# --------------------------------------------------------------------------- #

# How far below a "Customer Detail" heading that customer's block runs.
_BLOCK_DEPTH = 24


def _customer_block(page: Page) -> tuple[int, int] | None:
    """Where the customer's panel starts and ends, if the layout has one."""
    start = page.line_of("customer_name")
    if start is None:
        return None
    return start, min(start + _BLOCK_DEPTH, len(page.lines))


def _parties(page: Page, text: str) -> dict:
    """Both sides of the document, and which GSTIN belongs to whom.

    An invoice prints "Name" and "GSTIN" twice - once for each party - so the
    label alone cannot say which is meant. Position can: the customer's are
    the ones inside the customer panel, and the supplier's are whatever is
    left over.
    """
    block = _customer_block(page)
    customer_name = customer_gstin = customer_address = None

    if block:
        start, end = block
        # The bare "Name" inside the panel first: on a layout where the panel
        # heading is "Customer Detail", the heading's own value is the whole
        # line beneath it, labels and neighbouring fields included.
        customer_name = page.value_between("entity_name", start, end) \
            or page.value_between("customer_name", start, end) \
            or page.value_between("supplier_name", start, end)
        raw_gstin = page.value_between("customer_gstin", start, end) \
            or page.value_between("gstin", start, end)
        if raw_gstin:
            found = GSTIN_PATTERN.search(raw_gstin.upper())
            customer_gstin = found.group(0) if found else None
        customer_address = page.value_between("address", start, end)

    explicit_supplier = page.value("supplier_gstin")
    if explicit_supplier:
        found = GSTIN_PATTERN.search(explicit_supplier.upper())
        supplier_gstin = found.group(0) if found else None
    else:
        # Whichever GSTIN on the page is not the customer's. The document's
        # own issuer prints theirs in the header, so first-wins is right.
        on_page = GSTIN_PATTERN.findall(text.upper())
        supplier_gstin = next((g for g in on_page if g != customer_gstin), None)

    supplier_name = page.value("supplier_name")
    if supplier_name and supplier_name == customer_name:
        supplier_name = None
    if not supplier_name:
        supplier_name = _letterhead_name(page)

    return {
        "supplier_name": supplier_name,
        "supplier_gstin": supplier_gstin,
        "recipient_name": customer_name,
        "recipient_gstin": customer_gstin,
        "recipient_address": customer_address,
    }


def _letterhead_name(page: Page) -> str | None:
    """The first substantial line of the page, which is the issuer's name.

    Only used when nothing is labelled. Most invoices put the issuing
    business at the top of the page in larger type and never label it, so
    "the first real line" is a better answer than nothing - but it is a
    guess, and the confidence report grades it as one.
    """
    for line in page.lines[:6]:
        # The leftmost cell, not the whole line. In layout-preserved text a
        # line spans the page, so the letterhead shares its line with whatever
        # is printed on the right-hand side of the page - a phone number, a
        # heading - and taking the line would take all of it.
        cell = re.split(r"\s{2,}", line.strip())[0].strip()
        if not cell or is_noise(cell) or len(cell) < 3 or len(cell) > 60:
            continue
        if GSTIN_PATTERN.search(cell.upper()) or re.match(r"^[\d\W]+$", cell):
            continue
        if ":" in cell:      # "Phone : 8555875494" is a labelled field, not a name
            continue
        return cell
    return None


# --------------------------------------------------------------------------- #
# Odds and ends
# --------------------------------------------------------------------------- #

def _hsn(page: Page, items: list[lineitems.LineItem]) -> str | None:
    """The document's HSN code, preferring the item table's.

    Where the rows carry more than one, the first is returned and the
    confidence report flags that a single code cannot describe the invoice.
    """
    codes = lineitems.distinct_hsn(items)
    if codes:
        return codes[0]
    labelled = page.value("hsn")
    if labelled:
        candidate = labelled.split()[0].strip()
        if HSN_PATTERN.match(candidate):
            return candidate
    return None


def _quantity(page: Page, items: list[lineitems.LineItem]) -> Decimal | None:
    values = [i.quantity for i in items if i.quantity is not None]
    if values:
        return sum(values, Decimal("0"))
    return page.amount("quantity")


def _rate(amounts: amounts_reader.Amounts, items: list[lineitems.LineItem]) -> Decimal | None:
    """The invoice's GST rate.

    An item row's rate is preferred over one derived from the totals, and only
    where every row agrees. Where they disagree the derived figure is returned
    instead - a number between the two rates, which is exactly the evidence
    the validator needs to refuse the document.
    """
    distinct = lineitems.distinct_rates(items)
    if len(distinct) == 1:
        return distinct[0]
    return amounts_reader.rate_percent(amounts)


def _as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _model_items(items: list[lineitems.LineItem]) -> list[ModelLineItem]:
    return [
        ModelLineItem(
            description=i.description,
            hsn_sac=i.hsn,
            quantity=_as_float(i.quantity),
            unit_rate=_as_float(i.unit_rate),
            taxable_value=_as_float(i.taxable_value),
            gst_rate_percent=_as_float(i.gst_rate_percent),
        )
        for i in items
    ]


# --------------------------------------------------------------------------- #
# The read
# --------------------------------------------------------------------------- #

def _text_for(path: Path) -> tuple[str, str, str]:
    """Plain text, layout-preserved text, and where they came from."""
    layout = pdftext.document_layout_text(path)
    plain = pdftext.document_text(path)
    if not plain.strip() and not layout.strip():
        return "", "", "nothing"
    embedded = pdftext.pdf_text(path)
    source = "text layer" if len(embedded.strip()) >= ocr.MEANINGFUL_TEXT else "OCR"
    if path.suffix.lower() not in {".pdf"} and path.suffix.lower() in ocr.IMAGE_SUFFIXES:
        source = "OCR"
    return plain, (layout or plain), source


def read(path: Path) -> tuple[ExtractedInvoice, Report]:
    """Read a document, and say how much of it can be trusted."""
    plain, layout, source = _text_for(path)

    if source == "nothing":
        return _unreadable(path), Report([], "none", "nothing", [ocr.unavailable_note(path)])

    template = templates.identify(layout)
    page = Page(layout, templates.label_aliases(template, FIELD_ALIASES))

    items = lineitems.parse(layout, templates.column_aliases(template, lineitems.ROLES))
    amounts = amounts_reader.read(page, items)

    parties = _parties(page, layout)
    place = page.value("place_of_supply")
    invoice_date = page.value("invoice_date")
    rate = _rate(amounts, items)
    hsn = _hsn(page, items)

    extracted = ExtractedInvoice(
        document_type=None,
        invoice_number=page.value("invoice_number"),
        invoice_date=invoice_date,
        supplier_address=None,
        place_of_supply=place,
        reverse_charge=_reverse_charge(layout) or _reverse_charge(plain),
        is_credit_note=bool(CREDIT_NOTE_PATTERN.search(plain or layout)),
        hsn_sac=hsn,
        line_items=_model_items(items),
        taxable_value=_as_float(amounts.taxable),
        gst_rate_percent=_as_float(rate),
        cgst_amount=_as_float(amounts.cgst),
        sgst_amount=_as_float(amounts.sgst),
        igst_amount=_as_float(amounts.igst),
        cess_amount=_as_float(amounts.cess),
        total_amount=_as_float(amounts.total),
        quantity=_as_float(_quantity(page, items)),
        notes=None,
        **parties,
    )

    report = _report(extracted, page, items, amounts, source, template, place)
    extracted.notes = _note(report, amounts, items, template)
    return extracted, report


def _report(extracted, page, items, amounts, source, template, place) -> Report:
    from ..workbook import parse_date

    code = resolve_place_of_supply(place) if place else None

    return assess(
        invoice_number=extracted.invoice_number,
        invoice_date=extracted.invoice_date,
        invoice_date_parsed=parse_date(extracted.invoice_date),
        supplier_gstin=extracted.supplier_gstin,
        customer_gstin=extracted.recipient_gstin,
        supplier_name=extracted.supplier_name,
        customer_name=extracted.recipient_name,
        place_of_supply=place,
        place_of_supply_code=code,
        hsn=extracted.hsn_sac,
        rate=Decimal(str(extracted.gst_rate_percent)) if extracted.gst_rate_percent else None,
        amounts=amounts,
        items=items,
        source="OCR" if source == "OCR" else "text layer",
        is_sale=False,
    )


def _note(report: Report, amounts, items, template) -> str:
    """What a reviewer needs told, in the order they need it."""
    parts = [f"Read offline from the {report.source}."]
    if template:
        parts.append(templates.describe(template) + ".")
    if items:
        rates = lineitems.distinct_rates(items)
        detail = f"{len(items)} item row{'s' if len(items) > 1 else ''} read"
        if len(rates) > 1:
            detail += f", carrying {len(rates)} different GST rates"
        parts.append(detail + ".")
    if amounts.rounding_inferred:
        parts.append(f"A round-off of {amounts.round_off} was inferred to make the invoice balance.")
    if not amounts.balanced and amounts.residual is not None:
        parts.append(f"The invoice does not add up - {amounts.residual} is unaccounted for.")
    parts.extend(report.notes)
    weak = report.weak()
    if weak:
        parts.append("Check: " + "; ".join(f"{f.name} ({f.reason})" for f in weak[:4]) + ".")
    return " ".join(parts)


def _unreadable(path: Path) -> ExtractedInvoice:
    return ExtractedInvoice(
        document_type=None, invoice_number=None, invoice_date=None,
        supplier_name=None, supplier_gstin=None, supplier_address=None,
        recipient_name=None, recipient_gstin=None, recipient_address=None,
        place_of_supply=None, reverse_charge=False, is_credit_note=False,
        hsn_sac=None, line_items=[], taxable_value=None, gst_rate_percent=None,
        cgst_amount=None, sgst_amount=None, igst_amount=None, cess_amount=None,
        total_amount=None, quantity=None, notes=ocr.unavailable_note(path),
    )


def extract(path: Path) -> ExtractedInvoice:
    """The reader's public entry point, unchanged for existing callers."""
    return read(path)[0]
