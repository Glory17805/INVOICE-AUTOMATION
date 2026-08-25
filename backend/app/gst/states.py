"""GST state codes.

The first two characters of a GSTIN are the state code, and comparing the
supplier's state code against the place of supply is what decides CGST+SGST
versus IGST. That comparison is the one judgment call this system takes off a
person's desk, so the lookup needs to be exact.

Note on code 28: it was Andhra Pradesh before the 2014 bifurcation and is now
Andhra Pradesh (New). Some invoice templates - including Ira Innovations' own -
still print "Andhra Pradesh ** ( 28 )" while the GSTIN says 37. Both codes
resolve to Andhra Pradesh here, so a legacy printed code does not silently turn
an intra-state sale into an inter-state one.
"""

from __future__ import annotations

STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh",  # legacy code, pre-bifurcation
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
    "99": "Centre Jurisdiction",
}

# Codes that name the same state. Keyed by canonical code.
_EQUIVALENT_CODES: dict[str, set[str]] = {
    "37": {"28", "37"},
    "28": {"28", "37"},
}

_NAME_TO_CODE: dict[str, str] = {}
for _code, _name in STATE_CODES.items():
    # Later entries win, so 37 becomes the canonical code for Andhra Pradesh.
    _NAME_TO_CODE[_name.casefold()] = _code


def state_name(code: str | None) -> str | None:
    if not code:
        return None
    return STATE_CODES.get(str(code).strip().zfill(2))


def state_code_from_gstin(gstin: str | None) -> str | None:
    if not gstin:
        return None
    cleaned = gstin.strip().upper()
    if len(cleaned) < 2 or not cleaned[:2].isdigit():
        return None
    return cleaned[:2]


def resolve_place_of_supply(value: str | None) -> str | None:
    """Turn a free-text place of supply into a canonical state code.

    Handles the shapes that actually appear on invoices, in priority order:
    a state name, a "Name ( NN )" combination, or a bare numeric code. The name
    wins over the number so that a legacy printed code cannot override a
    correctly spelled state.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    cleaned = text.replace("*", " ")
    lowered = cleaned.casefold()

    # Longest name first so "Andhra Pradesh" is not shadowed by a shorter match.
    for name in sorted(_NAME_TO_CODE, key=len, reverse=True):
        if name in lowered:
            return _NAME_TO_CODE[name]

    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if digits:
        code = digits[:2].zfill(2)
        if code in STATE_CODES:
            return code
    return None


def same_state(code_a: str | None, code_b: str | None) -> bool:
    """True when two state codes name the same state."""
    if not code_a or not code_b:
        return False
    a = str(code_a).strip().zfill(2)
    b = str(code_b).strip().zfill(2)
    if a == b:
        return True
    return b in _EQUIVALENT_CODES.get(a, set())
