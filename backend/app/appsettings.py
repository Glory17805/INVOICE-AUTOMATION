"""Preferences a user can change from the Settings screen.

Distinct from `config.py`, deliberately. Config is deployment wiring - where the
workbook lives, which port, which key - set by whoever installs the thing and
read from the environment. This is what the person *using* it can change while
it runs, so it lives in the database and takes effect on the next request.

Unknown keys are rejected rather than stored. A settings table that accepts
anything becomes a junk drawer nobody dares clean out.
"""

from __future__ import annotations

import json

from . import db

# key -> (default, validator). The validator returns the cleaned value or
# raises ValueError with something worth showing a person.
DEFAULTS: dict[str, object] = {
    # Who may create an account from the sign-in page.
    #
    #   approval - anyone may ask; an administrator lets them in   (default)
    #   open     - anyone may create a working account immediately
    #   closed   - no signup link; administrators add people
    #
    # "approval" is the default rather than "open" because this system holds a
    # company's filed tax records. The link is there and it works; what it does
    # not do is hand out access to those records to whoever finds the page.
    "signup_mode": "approval",

    # Invoice processing
    "currency": "INR",
    "date_format": "dd-MMM-yyyy",
    "auto_post_clean": False,

    # Email intake
    "email_enabled": True,
    "email_process_pdf_attachments": True,
    "email_mark_processed": True,
    "email_notify": False,

    # Notifications
    "notify_on_complete": True,
    "notify_on_failure": True,
    "notify_on_review": True,
}

_CURRENCIES = {"INR", "USD", "EUR", "GBP", "AED", "SGD"}
_DATE_FORMATS = {"dd-MMM-yyyy", "dd/MM/yyyy", "yyyy-MM-dd", "MM/dd/yyyy"}
_SIGNUP_MODES = {"approval", "open", "closed"}


def _clean(key: str, value):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if key == "currency":
        text = str(value).strip().upper()
        if text not in _CURRENCIES:
            raise ValueError(f"Currency must be one of {', '.join(sorted(_CURRENCIES))}.")
        return text
    if key == "date_format":
        text = str(value).strip()
        if text not in _DATE_FORMATS:
            raise ValueError(f"Date format must be one of {', '.join(sorted(_DATE_FORMATS))}.")
        return text
    if key == "signup_mode":
        text = str(value).strip().lower()
        if text not in _SIGNUP_MODES:
            raise ValueError(f"Signup mode must be one of {', '.join(sorted(_SIGNUP_MODES))}.")
        return text
    return str(value).strip()


def all_settings() -> dict:
    values = dict(DEFAULTS)
    with db.LOCK, db.connect() as c:
        for row in c.execute("SELECT key, value FROM settings").fetchall():
            if row["key"] in DEFAULTS:
                try:
                    values[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    pass
    return values


def get(key: str):
    return all_settings().get(key, DEFAULTS.get(key))


def update(changes: dict) -> dict:
    """Apply a partial update. Unknown keys are an error, not a silent no-op."""
    unknown = [key for key in changes if key not in DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}.")

    cleaned = {key: _clean(key, value) for key, value in changes.items()}
    with db.LOCK, db.connect() as c:
        for key, value in cleaned.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
    return all_settings()


def options() -> dict:
    """What the Settings screen offers, so the choices live in one place."""
    return {
        "currency": sorted(_CURRENCIES),
        "date_format": sorted(_DATE_FORMATS),
        "signup_mode": [
            {"value": "approval",
             "label": "Anyone can ask; an administrator approves them"},
            {"value": "open",
             "label": "Anyone can create a working account immediately"},
            {"value": "closed",
             "label": "No signup link; administrators add people"},
        ],
    }
