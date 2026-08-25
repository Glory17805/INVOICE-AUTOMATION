"""GST return periods, derived rather than configured.

A GST return is monthly, and which return an invoice belongs to is a property
of the invoice - its own date - not a setting someone has to remember to change
each month. Configuring the period would mean the application only works for
whichever month it was last pointed at, and would silently file July sales into
a May return whenever someone forgot.

So nothing here is configurable. A period is computed from an invoice date, and
the workbook that period is written into is created on demand.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# "May-26", "Jul-26" - the form the workbook itself uses in its header row.
PERIOD_RE = re.compile(r"^([A-Za-z]{3})-(\d{2})$")

# Accepts the header cell's full text: "Return Period : May-26".
HEADER_RE = re.compile(r"return\s*period\s*[:\-]?\s*([A-Za-z]{3,9})[-\s/]+(\d{2,4})", re.I)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_NAMES = {value: key.capitalize() for key, value in _MONTHS.items()}


def period_of(value: date | datetime) -> str:
    """The return period an invoice dated `value` belongs to."""
    return f"{_NAMES[value.month]}-{value.strftime('%y')}"


def parse_period(text: str | None) -> tuple[int, int] | None:
    """Turn 'May-26' into (2026, 5). Returns None if it is not a period."""
    if not text:
        return None
    match = PERIOD_RE.match(str(text).strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(1).casefold())
    if not month:
        return None
    return 2000 + int(match.group(2)), month


def period_from_header(text: str | None) -> str | None:
    """Read a period out of a workbook header like 'Return Period : May-26'."""
    if not text:
        return None
    match = HEADER_RE.search(str(text))
    if not match:
        return None
    month = _MONTHS.get(match.group(1)[:3].casefold())
    if not month:
        return None
    year = int(match.group(2))
    if year > 99:
        year %= 100
    return f"{_NAMES[month]}-{year:02d}"


def header_text(period: str) -> str:
    """The header line a workbook for this period should carry."""
    return f"Return Period : {period}"


def is_valid(period: str | None) -> bool:
    return parse_period(period) is not None


def previous(period: str) -> str | None:
    """The period immediately before this one, for carry-forward lookups."""
    parsed = parse_period(period)
    if parsed is None:
        return None
    year, month = parsed
    if month == 1:
        year, month = year - 1, 12
    else:
        month -= 1
    return f"{_NAMES[month]}-{year % 100:02d}"


def sort_key(period: str) -> tuple[int, int]:
    """Chronological ordering for a list of periods."""
    return parse_period(period) or (0, 0)


def slug(period: str) -> str:
    """Filesystem-safe form, used for the per-period workbook filename."""
    return period.replace("/", "-").replace(" ", "")
