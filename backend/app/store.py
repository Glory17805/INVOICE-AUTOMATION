"""The invoice queue.

Every document is tracked from the moment it arrives to the moment it is
posted - the "nothing slips through" promise in the proposal. The workbook
remains the system of record; this holds only the queue around it.

This was a single JSON file, which is a fine shape for a queue and a poor one
for a growing one: every read parsed the whole file and every write rewrote it,
so the cost of touching one document scaled with how many had ever arrived. It
now lives in the shared SQLite database (see db.py). The interface below is
unchanged, so nothing above this module knows the difference.

An existing store.json is imported on first use and renamed, never deleted.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import db
from .config import STORE_PATH

# Databases whose legacy JSON import has already been considered this run.
_imported: set[str] = set()

# Kept as a module attribute so the tests that reach for it still work.
_prepared = _imported


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _upsert(connection, document: dict) -> None:
    connection.execute(
        "INSERT INTO documents (id, received_at, status, period, data) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  received_at=excluded.received_at, status=excluded.status, "
        "  period=excluded.period, data=excluded.data",
        (
            document["id"],
            document.get("received_at") or _now(),
            document.get("status"),
            document.get("period"),
            json.dumps(document, ensure_ascii=False),
        ),
    )


def _import_legacy_json(connection) -> None:
    """Carry a pre-existing store.json into the database, once.

    Only ever runs against an empty table, so re-running it cannot duplicate a
    queue, and the original file is renamed rather than removed - if this
    misreads something, the source is still on disk.
    """
    key = str(db.path())
    if key in _imported:
        return
    _imported.add(key)

    legacy = Path(STORE_PATH)
    if not legacy.exists():
        return
    if connection.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
        return

    try:
        documents = json.loads(legacy.read_text(encoding="utf-8")).get("documents", [])
    except (json.JSONDecodeError, OSError):
        return

    for document in documents:
        _upsert(connection, document)
    connection.commit()
    legacy.replace(legacy.with_suffix(".json.imported"))


def _open():
    """A connection with the legacy import already considered."""
    return db.connect()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def all_documents() -> list[dict]:
    with db.LOCK, _open() as c:
        _import_legacy_json(c)
        rows = c.execute(
            "SELECT data FROM documents ORDER BY received_at DESC, id DESC"
        ).fetchall()
    return [json.loads(row["data"]) for row in rows]


def get(doc_id: str) -> dict | None:
    with db.LOCK, _open() as c:
        _import_legacy_json(c)
        row = c.execute("SELECT data FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def add(document: dict) -> dict:
    document.setdefault("id", new_id())
    document.setdefault("received_at", _now())
    document.setdefault("history", [])
    document["history"].append({"at": _now(), "event": "received"})
    with db.LOCK, _open() as c:
        _import_legacy_json(c)
        _upsert(c, document)
    return document


def update(doc_id: str, changes: dict, event: str | None = None,
           actor: dict | None = None) -> dict | None:
    """Merge changes into a document, optionally recording who caused them."""
    with db.LOCK, _open() as c:
        _import_legacy_json(c)
        row = c.execute("SELECT data FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return None
        document = json.loads(row["data"])
        document.update(changes)
        if event:
            entry = {"at": _now(), "event": event}
            if actor:
                entry["by"] = actor.get("email")
                entry["by_name"] = actor.get("name")
            document.setdefault("history", []).append(entry)
        _upsert(c, document)
    return document


def delete(doc_id: str) -> bool:
    with db.LOCK, _open() as c:
        _import_legacy_json(c)
        return c.execute("DELETE FROM documents WHERE id = ?", (doc_id,)).rowcount > 0


def clear() -> None:
    with db.LOCK, _open() as c:
        c.execute("DELETE FROM documents")


def counts_by_status() -> dict[str, int]:
    """Straight from the index, rather than by loading every document."""
    with db.LOCK, _open() as c:
        _import_legacy_json(c)
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows if row["status"]}
