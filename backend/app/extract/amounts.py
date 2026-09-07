"""What an invoice adds up to, including the parts that are not tax.

The reader this replaces understood exactly one shape:

    taxable + CGST + SGST + IGST = total

and it insisted the sum closed to within five paise before it would believe
any of the figures. That check was right to exist - it is what stopped the
reader inventing numbers - but the shape was too narrow, so a bill carrying a
round-off line, freight, packing, a discount or cess failed it and every
amount on the invoice was thrown away together.

That is not a hypothetical. The invoices already in this system total
12,605.94 in their item rows and 12,606.00 on the payable line: a six-paise
round-off, one paisa past the old tolerance.

So this reads the whole shape:

    taxable
      + freight + packing + other charges
      - discount
      + CGST + SGST + IGST + cess
      + round-off
      = total

and, where a residual is small enough to be a rounding adjustment the invoice
did not print, records it as one instead of rejecting the read. A residual too
large for that is reported rather than swallowed - the caller decides, and the
figures survive either way so a reviewer can see what was read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .labels import Page
from .lineitems import LineItem

# What counts as agreement between the invoice's own figures and ours.
EXACT = Decimal("0.05")

# The most an unstated residual may be before it stops being a rounding
# adjustment. Indian invoices round to the rupee, so anything inside a rupee
# is a round-off; beyond that something has genuinely not been read.
ROUNDING = Decimal("1.00")

# Charges that are added to the taxable value, and the reliefs subtracted.
ADDITIONS = ("freight", "packing", "other_charges")
DEDUCTIONS = ("discount",)
TAXES = ("cgst", "sgst", "igst", "cess")


def _zero(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal("0")


@dataclass
class Amounts:
    """Every money figure on an invoice, and whether they agree."""

    taxable: Decimal | None = None
    freight: Decimal | None = None
    packing: Decimal | None = None
    other_charges: Decimal | None = None
    discount: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    cess: Decimal | None = None
    round_off: Decimal | None = None
    total: Decimal | None = None

    # How each figure was arrived at, for the confidence report.
    sources: dict[str, str] = field(default_factory=dict)
    residual: Decimal | None = None
    balanced: bool = False
    rounding_inferred: bool = False

    @property
    def total_tax(self) -> Decimal:
        return sum((_zero(getattr(self, name)) for name in TAXES), Decimal("0"))

    @property
    def charges(self) -> Decimal:
        return sum((_zero(getattr(self, name)) for name in ADDITIONS), Decimal("0"))

    @property
    def reliefs(self) -> Decimal:
        return sum((_zero(getattr(self, name)) for name in DEDUCTIONS), Decimal("0"))

    def expected_total(self) -> Decimal:
        return (_zero(self.taxable) + self.charges - self.reliefs
                + self.total_tax + _zero(self.round_off))

    def has_extras(self) -> bool:
        """Whether anything beyond taxable value and tax is on this bill."""
        return any(getattr(self, name) for name in (*ADDITIONS, *DEDUCTIONS, "cess"))

    def as_dict(self) -> dict:
        out = {}
        for name in ("taxable", "freight", "packing", "other_charges", "discount",
                     "cgst", "sgst", "igst", "cess", "round_off", "total"):
            value = getattr(self, name)
            out[name] = str(value) if value is not None else None
        out["residual"] = str(self.residual) if self.residual is not None else None
        out["balanced"] = self.balanced
        return out


def _from_items(items: list[LineItem]) -> Amounts:
    """Totals built by adding up the item rows.

    Used where the invoice prints no summary block, and as the cross-check
    where it prints one.
    """
    if not items:
        return Amounts()

    def total_of(attr: str) -> Decimal | None:
        values = [getattr(i, attr) for i in items if getattr(i, attr) is not None]
        return sum(values, Decimal("0")) if values else None

    return Amounts(
        taxable=total_of("taxable_value"),
        discount=total_of("discount"),
        cgst=total_of("cgst"),
        sgst=total_of("sgst"),
        igst=total_of("igst"),
        cess=total_of("cess"),
        total=total_of("total"),
        sources=dict.fromkeys(("taxable", "discount", "cgst", "sgst", "igst", "cess", "total"), "item rows"),
    )


def _reconcile(amounts: Amounts) -> Amounts:
    """Settle the arithmetic, inferring an unprinted round-off if that is all
    that stands between the parts and the whole."""
    if amounts.taxable is None or amounts.total is None:
        amounts.balanced = False
        return amounts

    residual = amounts.total - amounts.expected_total()
    amounts.residual = residual

    if abs(residual) <= EXACT:
        amounts.balanced = True
        return amounts

    if amounts.round_off is None and abs(residual) <= ROUNDING:
        amounts.round_off = residual
        amounts.rounding_inferred = True
        amounts.sources["round_off"] = "inferred from the residual"
        amounts.residual = Decimal("0")
        amounts.balanced = True
        return amounts

    amounts.balanced = False
    return amounts


def read(page: Page, items: list[LineItem] | None = None) -> Amounts:
    """Every amount on the invoice, from the summary block and the item rows.

    The summary block is preferred where it exists, because each figure there
    is named rather than inferred from position. The item rows fill anything
    it does not print - and where both exist and disagree, the disagreement is
    left visible in `residual` rather than resolved by picking a favourite.
    """
    items = items or []
    amounts = Amounts()

    for name in ("taxable", "freight", "packing", "other_charges", "discount",
                 "cgst", "sgst", "igst", "cess", "round_off", "total"):
        field_name = "taxable_value" if name == "taxable" else name
        value = page.amount(field_name)
        if value is not None:
            setattr(amounts, name, value)
            amounts.sources[name] = "summary block"

    from_items = _from_items(items)
    for name in ("taxable", "discount", "cgst", "sgst", "igst", "cess", "total"):
        if getattr(amounts, name) is None and getattr(from_items, name) is not None:
            setattr(amounts, name, getattr(from_items, name))
            amounts.sources[name] = "item rows"

    # A discount printed as a positive number is still a deduction; a negative
    # one has already been signed, and negating it again would add it back.
    if amounts.discount is not None and amounts.discount < 0:
        amounts.discount = -amounts.discount

    return _reconcile(amounts)


def rate_percent(amounts: Amounts) -> Decimal | None:
    """The single GST rate these amounts imply, if they imply one.

    Deliberately not snapped to a statutory slab. An invoice carrying two
    rates produces a number between them, and that number is the evidence
    that it carries two - rounding it to the nearest legal rate would destroy
    the only signal there is.
    """
    # GST is charged on what is actually being supplied for: freight and
    # packing are part of the consideration and a discount reduces it. Dividing
    # by the bare taxable value instead makes an ordinary 18% bill with freight
    # on it read as 18.72%, which would be flagged as an impossible rate.
    base = _zero(amounts.taxable) + amounts.charges - amounts.reliefs
    if base <= 0:
        return None
    # Cess is excluded: it is levied on top of GST at its own rate, and adding
    # it in would inflate every rate it appears on.
    tax = _zero(amounts.cgst) + _zero(amounts.sgst) + _zero(amounts.igst)
    if tax <= 0:
        return None
    return (tax / base * 100).quantize(Decimal("0.01"))
