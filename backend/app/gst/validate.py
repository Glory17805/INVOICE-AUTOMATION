"""Validation checks that run before anything is allowed to reach the workbook.

Stage 5 of the blueprint pipeline: GSTIN checksum, arithmetic reconciliation
against the tax stated on the document, and duplicate invoice detection within
the return period. A failure never drops a document - it routes it to Quick
Review with the reason attached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .states import STATE_CODES

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
_CHECKSUM_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Tolerance for reconciling stated tax against recomputed tax. Invoices round
# each line before totalling, so exact equality is the wrong bar.
TAX_TOLERANCE = Decimal("1.00")


@dataclass
class Issue:
    """One thing a person needs to look at."""

    code: str
    message: str
    severity: str = "error"  # "error" blocks auto-posting, "warning" does not

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def add(self, code: str, message: str, severity: str = "error") -> None:
        self.issues.append(Issue(code, message, severity))

    def as_dicts(self) -> list[dict]:
        return [i.as_dict() for i in self.issues]


def gstin_checksum_char(first_fourteen: str) -> str:
    """Compute the 15th GSTIN character from the first 14.

    Standard GST mod-36 scheme: each character's value is multiplied by an
    alternating factor of 1 and 2, the quotient and remainder of that product
    over 36 are summed, and the check character is 36 minus the total mod 36.
    """
    total = 0
    for index, char in enumerate(first_fourteen.upper()):
        value = _CHECKSUM_ALPHABET.index(char)
        factor = 1 if index % 2 == 0 else 2
        product = value * factor
        total += product // 36 + product % 36
    return _CHECKSUM_ALPHABET[(36 - total % 36) % 36]


def is_valid_gstin(gstin: str | None) -> bool:
    if not gstin:
        return False
    cleaned = gstin.strip().upper()
    if not GSTIN_RE.match(cleaned):
        return False
    if cleaned[:2] not in STATE_CODES:
        return False
    try:
        return gstin_checksum_char(cleaned[:14]) == cleaned[14]
    except ValueError:
        # A character outside the GSTIN alphabet.
        return False


def validate_gstin(result: ValidationResult, gstin: str | None, label: str, *, required: bool) -> None:
    """Check a GSTIN, if one is expected.

    An absent optional GSTIN is not an issue. Selling to an unregistered buyer
    is an ordinary B2C supply - the GSTR-1 sheet has a B2B/B2C split precisely
    because both are normal - so it is reported as context on the treatment
    rather than raised as something for a reviewer to resolve.
    """
    if not gstin:
        if required:
            result.add("gstin_missing", f"{label} GSTIN is missing.")
        return

    cleaned = gstin.strip().upper()
    if not GSTIN_RE.match(cleaned):
        result.add("gstin_format", f"{label} GSTIN {cleaned!r} is not a valid 15-character GSTIN.")
        return
    if cleaned[:2] not in STATE_CODES:
        result.add("gstin_state", f"{label} GSTIN starts with unknown state code {cleaned[:2]}.")
        return
    if gstin_checksum_char(cleaned[:14]) != cleaned[14]:
        result.add("gstin_checksum", f"{label} GSTIN {cleaned} fails its checksum - likely a misread digit.")


def reconcile_tax(
    result: ValidationResult,
    *,
    stated_total_tax: Decimal | None,
    computed_total_tax: Decimal,
    taxable_value: Decimal,
    rate: Decimal,
) -> None:
    """Check the document's own arithmetic against ours."""
    if taxable_value <= 0:
        result.add("taxable_value", "Taxable value is zero or missing.")
        return
    if rate <= 0:
        result.add("rate_missing", "No GST rate could be determined for this document.")
        return
    if stated_total_tax is None:
        result.add(
            "tax_not_stated",
            "The document does not state a tax total, so only our computed figure is available.",
            severity="warning",
        )
        return
    delta = abs(stated_total_tax - computed_total_tax)
    if delta > TAX_TOLERANCE:
        result.add(
            "tax_mismatch",
            f"Stated tax {stated_total_tax} differs from computed tax {computed_total_tax} "
            f"at {rate * 100:.2f}% (difference {delta}).",
        )


def check_duplicate(
    result: ValidationResult,
    *,
    invoice_no: str | None,
    party_gstin: str | None,
    existing: list[tuple[str, str | None]],
) -> None:
    """Flag an invoice number already posted for the same party this period."""
    if not invoice_no:
        result.add("invoice_no_missing", "Invoice number is missing.")
        return
    key = invoice_no.strip().casefold()
    party = (party_gstin or "").strip().upper()
    for seen_no, seen_party in existing:
        if seen_no.strip().casefold() != key:
            continue
        if not party or not seen_party or seen_party.strip().upper() == party:
            result.add(
                "duplicate_invoice",
                f"Invoice {invoice_no} is already posted in this return period.",
            )
            return
