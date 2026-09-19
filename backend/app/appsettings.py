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
    #   open   - anyone may create a working account immediately   (default)
    #   closed - no signup link; administrators add people
    #
    # There used to be a third mode, "approval", where an account was created
    # but could not sign in until an administrator let it in. It is gone: an
    # account either works or was never created. Someone holding credentials
    # that silently do nothing cannot tell that from a system that is broken.
    #
    # This system holds a company's filed tax records, so "open" is the right
    # default only while it is reachable by people who should have access -
    # a laptop, an office network. Exposed more widely than that, set it to
    # "closed" and add people from the Admin screen.
    "signup_mode": "open",

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
_SIGNUP_MODES = {"open", "closed"}

# A database written before the approval mode was removed may still hold it.
# Read as "open" rather than rejected: a value the validator refuses would
# leave the settings screen unable to load, and refusing to interpret an old
# value is not a reason to lock an administrator out of changing it.
_RETIRED_SIGNUP_MODES = {"approval": "open"}


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
        text = _RETIRED_SIGNUP_MODES.get(text, text)
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
    # Translated on the way out as well as on the way in, so a value stored
    # before the mode was retired never reaches a caller that has never heard
    # of it. Nothing is rewritten here: a read is not the place to write.
    stored = values.get("signup_mode")
    if stored in _RETIRED_SIGNUP_MODES:
        values["signup_mode"] = _RETIRED_SIGNUP_MODES[stored]
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
            {"value": "open",
             "label": "Anyone can create a working account immediately"},
            {"value": "closed",
             "label": "No signup link; administrators add people"},
        ],
    }
