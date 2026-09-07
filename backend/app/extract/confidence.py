"""How much to trust each field the reader produced.

A reader that returns a value and says nothing about it forces every caller
into the same false choice: treat a guess as a fact, or treat a fact as a
guess. This module says which it is, field by field, so the queue can send a
weak read to a person and let a strong one through.

Confidence here is evidence, never a feeling. Each level is earned by
something checkable - a GSTIN that passes its checksum, a date that parses, a
tax figure the invoice's own arithmetic agrees with, a rate that is one of the
seven that legally exist. Where there is no evidence either way the answer is
`MEDIUM`, which means "a person should look", not "probably fine".

The levels drive routing, so their meanings are operational:

  HIGH    checked against something independent, and it agreed
  MEDIUM  read cleanly, but nothing corroborates it
  LOW     read, and something about it is wrong or contradictory
  NONE    not found at all
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..gst.validate import STATUTORY_RATES, is_valid_gstin
from .amounts import Amounts
from .lineitems import LineItem

HIGH, MEDIUM, LOW, NONE = "high", "medium", "low", "none"

_ORDER = {NONE: 0, LOW: 1, MEDIUM: 2, HIGH: 3}

# Fields that must be right for a row to be postable at all. The overall
# verdict is the weakest of these; a shaky HSN code should not hold up an
# invoice whose money and parties are all confirmed.
CRITICAL = ("invoice_number", "invoice_date", "supplier_gstin",
            "taxable_value", "gst_rate_percent", "total_amount")


@dataclass(frozen=True)
class Field:
    """One field's verdict, and the evidence for it."""

    name: str
    level: str
    reason: str

    def as_dict(self) -> dict:
        return {"field": self.name, "level": self.level, "reason": self.reason}


def weakest(levels: list[str]) -> str:
    return min(levels, key=lambda level: _ORDER[level]) if levels else NONE


def _cap(level: str, ceiling: str) -> str:
    return level if _ORDER[level] <= _ORDER[ceiling] else ceiling


# --------------------------------------------------------------------------- #
# Per-field rules
# --------------------------------------------------------------------------- #

def _gstin(name: str, value: str | None, *, required: bool) -> Field:
    if not value:
        return Field(name, NONE if required else MEDIUM,
                     "not printed on the document" if required else "none printed, which is normal for a B2C sale")
    if is_valid_gstin(value):
        return Field(name, HIGH, "passes the GSTIN checksum")
    return Field(name, LOW, "fails the GSTIN checksum, so a character was misread")


def _date(value: str | None, parsed) -> Field:
    if not value:
        return Field("invoice_date", NONE, "not found")
    if parsed is None:
        return Field("invoice_date", LOW, f"{value!r} could not be read as a date")
    return Field("invoice_date", HIGH, f"reads as {parsed:%d-%b-%Y}")


def _invoice_number(value: str | None) -> Field:
    if not value:
        return Field("invoice_number", NONE, "not found")
    if len(value) > 40 or "\n" in value:
        return Field("invoice_number", LOW, "too long to be an invoice number - the label probably ran on")
    return Field("invoice_number", MEDIUM, "read from a labelled field")


def _party(name: str, value: str | None) -> Field:
    if not value:
        return Field(name, NONE, "not found")
    if len(value) < 3:
        return Field(name, LOW, "too short to be a name")
    return Field(name, MEDIUM, "read from a labelled field")


def _amount_fields(amounts: Amounts, items: list[LineItem]) -> list[Field]:
    """Money, judged on whether the invoice's own figures agree."""
    out: list[Field] = []

    if amounts.taxable is None:
        out.append(Field("taxable_value", NONE, "no taxable value could be read"))
    elif not amounts.balanced:
        out.append(Field("taxable_value", LOW,
                         f"the invoice does not add up - {amounts.residual} unaccounted for"))
    elif items and amounts.sources.get("taxable") == "summary block":
        rows = sum((i.taxable_value or Decimal("0")) for i in items)
        if abs(rows - amounts.taxable) <= Decimal("0.05"):
            out.append(Field("taxable_value", HIGH,
                             "the summary block and the item rows agree"))
        else:
            out.append(Field("taxable_value", LOW,
                             f"the summary says {amounts.taxable} but the rows add to {rows}"))
    else:
        out.append(Field("taxable_value", MEDIUM,
                         "the invoice balances, but there is only one source for it"))

    if amounts.total is None:
        out.append(Field("total_amount", NONE, "no total could be read"))
    elif amounts.balanced:
        detail = ("balances once the printed round-off is applied"
                  if not amounts.rounding_inferred
                  else f"balances with an inferred round-off of {amounts.round_off}")
        out.append(Field("total_amount", HIGH, detail))
    else:
        out.append(Field("total_amount", LOW, "does not match the parts it is made of"))

    return out


def _rate(rate: Decimal | None, items: list[LineItem]) -> Field:
    """The rate, judged against the rates that legally exist.

    A rate off every statutory slab is the signature of an invoice carrying
    more than one rate that has been read as carrying one.
    """
    if rate is None or rate <= 0:
        return Field("gst_rate_percent", NONE, "no rate could be determined")

    fraction = rate / 100
    on_slab = any(abs(fraction - slab) <= Decimal("0.005") for slab in STATUTORY_RATES)

    distinct = {i.gst_rate_percent for i in items
                if i.gst_rate_percent is not None and i.gst_rate_percent > 0}
    if len(distinct) > 1:
        listed = ", ".join(f"{r}%" for r in sorted(distinct))
        return Field("gst_rate_percent", LOW,
                     f"the item rows carry {len(distinct)} different rates ({listed}), "
                     "so one rate cannot describe this invoice")
    if not on_slab:
        return Field("gst_rate_percent", LOW,
                     f"{rate}% is not a GST rate, so the figures behind it were misread")
    if distinct:
        return Field("gst_rate_percent", HIGH, "every item row carries this rate")
    return Field("gst_rate_percent", MEDIUM, "derived from the amounts, with no item rows to confirm it")


def _hsn(value: str | None, items: list[LineItem]) -> Field:
    codes = {i.hsn for i in items if i.hsn}
    if not value and not codes:
        return Field("hsn_sac", NONE, "not printed")
    if len(codes) > 1:
        listed = ", ".join(sorted(codes))
        return Field("hsn_sac", LOW,
                     f"the invoice carries several HSN codes ({listed}) and only one can be filed")
    if value and value.isdigit() and 4 <= len(value) <= 8:
        return Field("hsn_sac", HIGH, "a well-formed HSN code, and the only one on the invoice")
    return Field("hsn_sac", MEDIUM, "read, but not in a shape that confirms it")


def _place(value: str | None, code: str | None) -> Field:
    if not value:
        return Field("place_of_supply", NONE, "not printed")
    if code:
        return Field("place_of_supply", HIGH, f"resolves to state code {code}")
    return Field("place_of_supply", LOW,
                 f"{value!r} does not resolve to a state, so the CGST/SGST vs IGST split is a guess")


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

@dataclass
class Report:
    fields: list[Field]
    overall: str
    source: str          # "text layer", "OCR", "template: <name>"
    notes: list[str]

    def level_of(self, name: str) -> str:
        return next((f.level for f in self.fields if f.name == name), NONE)

    def weak(self) -> list[Field]:
        """The fields a person should look at, worst first."""
        return sorted((f for f in self.fields if _ORDER[f.level] <= _ORDER[LOW]),
                      key=lambda f: _ORDER[f.level])

    def as_dict(self) -> dict:
        return {
            "overall": self.overall,
            "source": self.source,
            "fields": [f.as_dict() for f in self.fields],
            "notes": self.notes,
        }


def assess(*, invoice_number, invoice_date, invoice_date_parsed, supplier_gstin,
           customer_gstin, supplier_name, customer_name, place_of_supply,
           place_of_supply_code, hsn, rate, amounts: Amounts,
           items: list[LineItem], source: str, is_sale: bool) -> Report:
    """Judge every field, and the document as a whole."""
    fields = [
        _invoice_number(invoice_number),
        _date(invoice_date, invoice_date_parsed),
        _gstin("supplier_gstin", supplier_gstin, required=True),
        _gstin("recipient_gstin", customer_gstin, required=False),
        _party("supplier_name", supplier_name),
        _party("recipient_name", customer_name),
        _place(place_of_supply, place_of_supply_code),
        _hsn(hsn, items),
        _rate(rate, items),
        *_amount_fields(amounts, items),
    ]

    notes: list[str] = []

    # OCR reads digits less reliably than a text layer does, and every figure
    # here is digits. Nothing read from a scan is better than "look at it".
    if source == "OCR":
        fields = [Field(f.name, _cap(f.level, MEDIUM),
                        f.reason + "; read by OCR from a scan, so the characters are themselves a guess")
                  if f.level == HIGH else f
                  for f in fields]
        notes.append("Read by OCR. Check the figures against the document before posting.")

    if not items:
        notes.append("No item table could be read, so a bill carrying more than one "
                     "tax rate would not be detected as such.")

    overall = weakest([f.level for f in fields if f.name in CRITICAL])
    return Report(fields, overall, source, notes)
