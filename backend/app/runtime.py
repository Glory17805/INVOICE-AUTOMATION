"""What the reader actually did last, and whether that is still worth saying.

/api/info used to answer "which reader is really running?" by looking at the
newest document and reading the note left on it. That note records what happened
when the document was *read*, which can be days earlier and under entirely
different configuration - so adding a Gemini key produced a screen that said, at
the same time, "your invoices may be sent to Gemini" and "no Gemini key is
configured". Both sentences were generated from real data; one of them was
describing the past.

So the last read is recorded here with the configuration it happened under, and
reported only while that configuration still holds. When it has changed, the
honest answer is that nothing has been read since - not a stale sentence
presented in the present tense.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from . import db

# Kept in the settings table under a reserved prefix. appsettings only reads
# keys it knows about, so this cannot surface as a user-facing preference.
_KEY = "runtime.last_read"


def record_read(provider: str, had_credentials: bool, reader: str, note: str | None) -> None:
    """Remember the outcome of a read, and the configuration it ran under."""
    payload = {
        "provider": provider,
        "had_credentials": bool(had_credentials),
        "reader": reader,
        "note": note,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        with db.LOCK, db.connect() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_KEY, json.dumps(payload)),
            )
    except Exception:
        pass


def _stored() -> dict | None:
    try:
        with db.LOCK, db.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (_KEY,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def last_read(provider: str, had_credentials: bool) -> dict | None:
    """The last read, but only while it still describes the present.

    Returns None when the configuration has changed since - a different
    provider, or credentials added or removed. Nothing has been read under
    *this* setup yet, and saying so is better than repeating what was true
    under the old one.
    """
    stored = _stored()
    if stored is None:
        return None
    if stored.get("provider") != provider:
        return None
    if bool(stored.get("had_credentials")) != bool(had_credentials):
        return None
    return stored


def forget() -> None:
    """Drop the record. Used by tests and by a workbook reset."""
    try:
        with db.LOCK, db.connect() as c:
            c.execute("DELETE FROM settings WHERE key = ?", (_KEY,))
    except Exception:
        pass
