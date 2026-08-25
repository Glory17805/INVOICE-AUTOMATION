"""Stages 2 and 4 of the pipeline: classify the document, then tax it.

This module holds the judgment call the proposal is really about - deciding
whether a document is a sale, a purchase, a credit note, or a reverse-charge
expense, and whether its tax splits into CGST+SGST or lands wholly in IGST.
Everything here is deterministic; the LLM only supplies the fields it reads off
the page, never the tax treatment.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ..config import Company
from ..models import DocumentType, ExtractedInvoice, GstTreatment, SupplyType
from .states import resolve_place_of_supply, same_state, state_code_from_gstin, state_name

TWO_PLACES = Decimal("0.01")


def money(value) -> Decimal:
    """Coerce anything invoice-shaped into a 2dp Decimal."""
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _rate_fraction(percent) -> Decimal:
    """Normalise a rate that may arrive as 18, 18.0, or 0.18."""
    if percent is None:
        return Decimal("0")
    try:
        raw = Decimal(str(percent))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if raw <= 0:
        return Decimal("0")
    # A GST rate above 1 is a percentage; at or below 1 it is already a fraction.
    fraction = raw / Decimal("100") if raw > 1 else raw
    return fraction.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _is_us(company: Company, name: str | None, gstin: str | None) -> bool:
    if gstin and gstin.strip().upper() == company.gstin.upper():
        return True
    if name and company.normalised_name in name.strip().casefold():
        return True
    return False


def classify(doc: ExtractedInvoice, company: Company) -> tuple[DocumentType, str]:
    """Decide which of the four registers this document belongs on.

    Order matters. A credit note is a credit note whichever direction it points,
    and a reverse-charge purchase goes to the RCM register rather than the
    ordinary purchase register, because the RCM sheet has no CGST/SGST columns -
    the buyer pays that tax directly instead of the seller collecting it.
    """
    we_are_supplier = _is_us(company, doc.supplier_name, doc.supplier_gstin)
    we_are_recipient = _is_us(company, doc.recipient_name, doc.recipient_gstin)

    if doc.is_credit_note:
        return DocumentType.CREDIT_NOTE, "Document identifies itself as a credit note."

    if doc.reverse_charge and not we_are_supplier:
        return DocumentType.RCM, "Document is flagged reverse charge, so the tax is payable by Ira Innovations."

    if we_are_supplier and not we_are_recipient:
        return DocumentType.SALES, "Ira Innovations is the supplier on this invoice."

    if we_are_recipient and not we_are_supplier:
        return DocumentType.PURCHASE, "Ira Innovations is the recipient on this bill."

    # Neither side matched cleanly - fall back to the reader's own hint and say so.
    hint = (doc.document_type or "").strip().lower()
    for candidate in DocumentType:
        if hint == candidate.value:
            return candidate, "Neither party matched Ira Innovations exactly; used the reader's classification."
    return DocumentType.PURCHASE, "Could not identify either party as Ira Innovations; defaulted to purchase."


def determine_supply_type(
    doc: ExtractedInvoice,
    doc_type: DocumentType,
    company: Company,
) -> tuple[SupplyType, str | None, str | None]:
    """Compare supplier state against place of supply.

    Returns the supply type plus the two state codes that produced it, so the
    review screen can show a reviewer exactly what the decision was based on.
    """
    supplier_code = state_code_from_gstin(doc.supplier_gstin) or resolve_place_of_supply(doc.supplier_address)

    pos_code = resolve_place_of_supply(doc.place_of_supply)
    if not pos_code:
        # No place of supply printed. For a sale it is the customer's state; for
        # anything we buy, it is ours.
        if doc_type is DocumentType.SALES:
            pos_code = state_code_from_gstin(doc.recipient_gstin) or resolve_place_of_supply(doc.recipient_address)
        else:
            pos_code = company.state_code

    if doc_type is DocumentType.SALES and not supplier_code:
        supplier_code = company.state_code

    if not supplier_code or not pos_code:
        # Unknown on either side: assume the safer inter-state treatment, and the
        # validation layer will flag it for review.
        return SupplyType.INTER_STATE, supplier_code, pos_code

    supply = SupplyType.INTRA_STATE if same_state(supplier_code, pos_code) else SupplyType.INTER_STATE
    return supply, supplier_code, pos_code


def _taxable_value(doc: ExtractedInvoice) -> Decimal:
    if doc.taxable_value is not None:
        return money(doc.taxable_value)
    total = sum((money(li.taxable_value) for li in doc.line_items), Decimal("0.00"))
    return total.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _rate(doc: ExtractedInvoice) -> Decimal:
    rate = _rate_fraction(doc.gst_rate_percent)
    if rate > 0:
        return rate
    for line in doc.line_items:
        rate = _rate_fraction(line.gst_rate_percent)
        if rate > 0:
            return rate
    # Derive it from the stated tax when the document only prints amounts. The
    # sample sales invoice prints 9% + 9% per line rather than a combined rate.
    taxable = _taxable_value(doc)
    stated = money(doc.cgst_amount) + money(doc.sgst_amount) + money(doc.igst_amount)
    if taxable > 0 and stated > 0:
        derived = (stated / taxable).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        # Snap to the statutory slab it is closest to, within half a point.
        for slab in (Decimal("0.0025"), Decimal("0.005"), Decimal("0.03"), Decimal("0.05"),
                     Decimal("0.12"), Decimal("0.18"), Decimal("0.28")):
            if abs(derived - slab) <= Decimal("0.005"):
                return slab
        return derived
    return Decimal("0")


def apply_gst(doc: ExtractedInvoice, company: Company) -> GstTreatment:
    """Run classification and the tax split, and recompute the tax ourselves.

    The recomputation mirrors the workbook's own formulas: taxable value x rate
    / 2 into each of CGST and SGST for an intra-state supply, or the whole
    taxable value x rate into IGST for an inter-state one.
    """
    doc_type, reason = classify(doc, company)
    supply, supplier_code, pos_code = determine_supply_type(doc, doc_type, company)

    taxable = _taxable_value(doc)
    rate = _rate(doc)
    cess = money(doc.cess_amount)

    if supply is SupplyType.INTRA_STATE:
        half = (taxable * rate / Decimal("2")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        cgst, sgst, igst = half, half, Decimal("0.00")
    else:
        cgst = sgst = Decimal("0.00")
        igst = (taxable * rate).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    total_tax = (cgst + sgst + igst + cess).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if doc_type is DocumentType.RCM:
        # On a reverse-charge bill the supplier does not collect tax, so the
        # amount payable to them is the taxable value alone. The tax is a
        # separate liability, which is why the RCM sheet totals to the bill
        # value while still carrying an IGST figure.
        invoice_total = taxable
    else:
        invoice_total = (taxable + total_tax).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if doc_type is DocumentType.SALES:
        counterparty_name, counterparty_gstin = doc.recipient_name, doc.recipient_gstin
    else:
        counterparty_name, counterparty_gstin = doc.supplier_name, doc.supplier_gstin

    return GstTreatment(
        document_type=doc_type,
        supply_type=supply,
        supplier_state_code=supplier_code,
        supplier_state_name=state_name(supplier_code),
        place_of_supply_code=pos_code,
        place_of_supply_name=state_name(pos_code),
        rate=rate,
        taxable_value=taxable,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        cess=cess,
        total_tax=total_tax,
        invoice_total=invoice_total,
        counterparty_name=counterparty_name,
        counterparty_gstin=counterparty_gstin,
        supply_category="B2B" if counterparty_gstin else "B2C",
        classification_reason=reason,
    )


def stated_total_tax(doc: ExtractedInvoice) -> Decimal | None:
    """The tax the document itself claims, or None if it states none."""
    parts = [doc.cgst_amount, doc.sgst_amount, doc.igst_amount]
    if all(p is None for p in parts):
        return None
    return (money(doc.cgst_amount) + money(doc.sgst_amount) + money(doc.igst_amount)).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
