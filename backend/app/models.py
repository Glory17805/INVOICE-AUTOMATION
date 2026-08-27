"""Schemas shared by the extractor, the rules engine, the store, and the API."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    """The four destinations a document can be routed to."""

    SALES = "sales"
    PURCHASE = "purchase"
    CREDIT_NOTE = "credit_note"
    RCM = "rcm"

    @property
    def sheet(self) -> str:
        return {
            DocumentType.SALES: "GSTR-1",
            DocumentType.PURCHASE: "GSTR-2B",
            DocumentType.CREDIT_NOTE: "Credit Note",
            DocumentType.RCM: "RCM",
        }[self]

    @property
    def label(self) -> str:
        return {
            DocumentType.SALES: "Sale",
            DocumentType.PURCHASE: "Purchase",
            DocumentType.CREDIT_NOTE: "Credit note",
            DocumentType.RCM: "Reverse charge",
        }[self]


class SupplyType(str, Enum):
    INTRA_STATE = "intra_state"  # CGST + SGST
    INTER_STATE = "inter_state"  # IGST

    @property
    def label(self) -> str:
        return "CGST + SGST - same state" if self is SupplyType.INTRA_STATE else "IGST - different state"


class DocStatus(str, Enum):
    NEW = "new"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    POSTED = "posted"
    FAILED = "failed"


class LineItem(BaseModel):
    """One row of the invoice's product/service table."""

    description: str | None
    hsn_sac: str | None
    quantity: float | None
    unit_rate: float | None
    taxable_value: float | None
    gst_rate_percent: float | None


class ExtractedInvoice(BaseModel):
    """Exactly what the reader found on the document, before any GST rules run.

    This doubles as the JSON schema handed to Claude, so every description here
    is prompt surface - keep them precise and free of assumptions about which
    register the document belongs to.
    """

    document_type: str | None = Field(
        description=(
            "One of 'sales', 'purchase', 'credit_note', 'rcm' - your best read of what "
            "this document is from the perspective of Ira Innovations. This is a hint; "
            "leave it null if genuinely unclear."
        )
    )
    invoice_number: str | None = Field(description="Invoice, bill, or credit note number exactly as printed.")
    invoice_date: str | None = Field(description="Document date exactly as printed, e.g. '31-May-2026' or '23/12/2025'.")

    supplier_name: str | None = Field(description="Legal or trade name of the party issuing the document (the seller).")
    supplier_gstin: str | None = Field(description="15-character GSTIN of the supplier, uppercase, no spaces.")
    supplier_address: str | None = Field(description="Supplier address or state as printed.")

    recipient_name: str | None = Field(description="Name of the party being billed (the buyer/customer).")
    recipient_gstin: str | None = Field(description="15-character GSTIN of the recipient, or null if unregistered.")
    recipient_address: str | None = Field(description="Recipient address or state as printed.")

    place_of_supply: str | None = Field(
        description="Place of supply exactly as printed, e.g. 'Andhra Pradesh ( 28 )'. Null if not stated."
    )
    reverse_charge: bool | None = Field(
        description="True only if the document explicitly says reverse charge is applicable ('Yes')."
    )
    is_credit_note: bool | None = Field(
        description="True if this document is a credit note or a refund/adjustment note rather than a tax invoice."
    )

    hsn_sac: str | None = Field(description="Primary HSN or SAC code on the document.")
    line_items: list[LineItem] = Field(description="Every line of the product/service table. Empty list if none is present.")

    taxable_value: float | None = Field(description="Total taxable value before tax, as stated on the document.")
    gst_rate_percent: float | None = Field(description="GST rate as a percentage, e.g. 18 for 18%, 5 for 5%.")
    cgst_amount: float | None = Field(description="Central tax amount stated on the document, else null.")
    sgst_amount: float | None = Field(description="State tax amount stated on the document, else null.")
    igst_amount: float | None = Field(description="Integrated tax amount stated on the document, else null.")
    cess_amount: float | None = Field(description="Cess amount stated on the document, else null.")
    total_amount: float | None = Field(description="Invoice grand total including tax, as stated.")

    quantity: float | None = Field(description="Total quantity across all lines, if the document totals it.")
    notes: str | None = Field(description="Anything ambiguous or unusual a human reviewer should know. Null if nothing.")


class GstTreatment(BaseModel):
    """The rules engine's verdict: where the document goes and how it is taxed."""

    document_type: DocumentType
    supply_type: SupplyType
    supplier_state_code: str | None
    supplier_state_name: str | None
    place_of_supply_code: str | None
    place_of_supply_name: str | None
    rate: Decimal  # fraction, e.g. Decimal("0.18")
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    cess: Decimal
    total_tax: Decimal
    invoice_total: Decimal
    counterparty_name: str | None
    counterparty_gstin: str | None
    # "B2B" when the counterparty is GST-registered, "B2C" when they are not.
    # The GSTR-1 sheet splits its totals along exactly this line.
    supply_category: str
    classification_reason: str

    model_config = {"arbitrary_types_allowed": True}


class RegisterRow(BaseModel):
    """A row as it appears in one of the four registers."""

    sheet: str
    row: int
    values: dict[str, str | None]


class TaxPayableSummary(BaseModel):
    """Every figure is a decimal string, not a float - see workbook._floats."""

    itc_carry_forward: dict[str, str]
    itc_current_purchases: dict[str, str]
    credit_note_reversal: dict[str, str]
    rcm_input: dict[str, str]
    itc_available: dict[str, str]
    output_tax: dict[str, str]
    net_payable: dict[str, str]
    rcm_cash_payable: dict[str, str]
    return_period: str
