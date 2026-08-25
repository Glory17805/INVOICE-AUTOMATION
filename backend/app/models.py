"""Schemas shared by the extractor, the rules engine, the store, and the API."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

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

    description: Optional[str]
    hsn_sac: Optional[str]
    quantity: Optional[float]
    unit_rate: Optional[float]
    taxable_value: Optional[float]
    gst_rate_percent: Optional[float]


class ExtractedInvoice(BaseModel):
    """Exactly what the reader found on the document, before any GST rules run.

    This doubles as the JSON schema handed to Claude, so every description here
    is prompt surface - keep them precise and free of assumptions about which
    register the document belongs to.
    """

    document_type: Optional[str] = Field(
        description=(
            "One of 'sales', 'purchase', 'credit_note', 'rcm' - your best read of what "
            "this document is from the perspective of Ira Innovations. This is a hint; "
            "leave it null if genuinely unclear."
        )
    )
    invoice_number: Optional[str] = Field(description="Invoice, bill, or credit note number exactly as printed.")
    invoice_date: Optional[str] = Field(description="Document date exactly as printed, e.g. '31-May-2026' or '23/12/2025'.")

    supplier_name: Optional[str] = Field(description="Legal or trade name of the party issuing the document (the seller).")
    supplier_gstin: Optional[str] = Field(description="15-character GSTIN of the supplier, uppercase, no spaces.")
    supplier_address: Optional[str] = Field(description="Supplier address or state as printed.")

    recipient_name: Optional[str] = Field(description="Name of the party being billed (the buyer/customer).")
    recipient_gstin: Optional[str] = Field(description="15-character GSTIN of the recipient, or null if unregistered.")
    recipient_address: Optional[str] = Field(description="Recipient address or state as printed.")

    place_of_supply: Optional[str] = Field(
        description="Place of supply exactly as printed, e.g. 'Andhra Pradesh ( 28 )'. Null if not stated."
    )
    reverse_charge: Optional[bool] = Field(
        description="True only if the document explicitly says reverse charge is applicable ('Yes')."
    )
    is_credit_note: Optional[bool] = Field(
        description="True if this document is a credit note or a refund/adjustment note rather than a tax invoice."
    )

    hsn_sac: Optional[str] = Field(description="Primary HSN or SAC code on the document.")
    line_items: list[LineItem] = Field(description="Every line of the product/service table. Empty list if none is present.")

    taxable_value: Optional[float] = Field(description="Total taxable value before tax, as stated on the document.")
    gst_rate_percent: Optional[float] = Field(description="GST rate as a percentage, e.g. 18 for 18%, 5 for 5%.")
    cgst_amount: Optional[float] = Field(description="Central tax amount stated on the document, else null.")
    sgst_amount: Optional[float] = Field(description="State tax amount stated on the document, else null.")
    igst_amount: Optional[float] = Field(description="Integrated tax amount stated on the document, else null.")
    cess_amount: Optional[float] = Field(description="Cess amount stated on the document, else null.")
    total_amount: Optional[float] = Field(description="Invoice grand total including tax, as stated.")

    quantity: Optional[float] = Field(description="Total quantity across all lines, if the document totals it.")
    notes: Optional[str] = Field(description="Anything ambiguous or unusual a human reviewer should know. Null if nothing.")


class GstTreatment(BaseModel):
    """The rules engine's verdict: where the document goes and how it is taxed."""

    document_type: DocumentType
    supply_type: SupplyType
    supplier_state_code: Optional[str]
    supplier_state_name: Optional[str]
    place_of_supply_code: Optional[str]
    place_of_supply_name: Optional[str]
    rate: Decimal  # fraction, e.g. Decimal("0.18")
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    cess: Decimal
    total_tax: Decimal
    invoice_total: Decimal
    counterparty_name: Optional[str]
    counterparty_gstin: Optional[str]
    # "B2B" when the counterparty is GST-registered, "B2C" when they are not.
    # The GSTR-1 sheet splits its totals along exactly this line.
    supply_category: str
    classification_reason: str

    model_config = {"arbitrary_types_allowed": True}


class RegisterRow(BaseModel):
    """A row as it appears in one of the four registers."""

    sheet: str
    row: int
    values: dict[str, Optional[str]]


class TaxPayableSummary(BaseModel):
    itc_carry_forward: dict[str, float]
    itc_current_purchases: dict[str, float]
    credit_note_reversal: dict[str, float]
    rcm_input: dict[str, float]
    itc_available: dict[str, float]
    output_tax: dict[str, float]
    net_payable: dict[str, float]
    rcm_cash_payable: dict[str, float]
    return_period: str
