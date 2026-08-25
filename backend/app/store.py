"""A SQLite-backed document store.

Every document is tracked from the moment it arrives to the moment it is
posted - the "nothing slips through" promise in the proposal. The workbook
remains the system of record; this holds only the queue around it.

This was a single JSON file, which is a fine shape for a queue and a poor one
for a growing one. Every read parsed the whole file and every write rewrote it,
so the cost of touching one document scaled with how many had ever arrived -
and one print run is eighteen of them. It also meant a write was a
whole-file replace: fine until the process dies between `write` and `replace`.

SQLite fixes both without adding a service to run: a single file, in the
standard library, with real transactions and an index on the columns actually
queried. The public interface is unchanged, so nothing above this module knows
which one it is talking to.

An existing store.json is imported automatically on first use and kept, renamed,
rather than deleted.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .config import STORE_PATH, ensure_dirs

_LOCK = RLock()

# `status` and `period` are lifted out of the JSON into real columns because
# they are what the inbox filters and groups by. Everything else stays in the
# blob: this is a queue, not a reporting database, and a migration per field
# added to a document would be a poor trade.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    status      TEXT,
    period      TEXT,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_received_at ON documents(received_at DESC);
CREATE INDEX IF NOT EXISTS documents_status      ON documents(status);
CREATE INDEX IF NOT EXISTS documents_period      ON documents(period);
"""

# Paths whose schema and JSON import have already been handled this run.
_prepared: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    """The database file, named after the JSON store it replaces.

    Deriving it from STORE_PATH keeps a single knob: the tests point STORE_PATH
    at a temporary directory and get an isolated database for free.
    """
    return Path(STORE_PATH).with_suffix(".db")


def _prepare(connection: sqlite3.Connection, path: Path) -> None:
    connection.executescript(_SCHEMA)
    key = str(path)
    if key in _prepared:
        return
    _import_json(connection, path)
    _prepared.add(key)


def _import_json(connection: sqlite3.Connection, path: Path) -> None:
    """Carry a pre-existing store.json into the database, once.

    Only ever runs against an empty table, so re-running it cannot duplicate a
    queue, and the original file is renamed rather than removed - if this
    misreads something, the source is still on disk.
    """
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


@contextmanager
def _connect():
    ensure_dirs()
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        # WAL lets a reader and a writer overlap instead of blocking, which is
        # what the inbox polling does against a background read.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        _prepare(connection, path)
        yield connection
        connection.commit()
    finally:
        connection.close()


def _upsert(connection: sqlite3.Connection, document: dict) -> None:
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


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def all_documents() -> list[dict]:
    with _LOCK, _connect() as connection:
        rows = connection.execute(
            "SELECT data FROM documents ORDER BY received_at DESC, id DESC"
        ).fetchall()
    return [json.loads(row["data"]) for row in rows]


def get(doc_id: str) -> dict | None:
    with _LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT data FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else None


def add(document: dict) -> dict:
    document.setdefault("id", new_id())
    document.setdefault("received_at", _now())
    document.setdefault("history", [])
    document["history"].append({"at": _now(), "event": "received"})
    with _LOCK, _connect() as connection:
        _upsert(connection, document)
    return document


def update(doc_id: str, changes: dict, event: str | None = None) -> dict | None:
    with _LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT data FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        document = json.loads(row["data"])
        document.update(changes)
        if event:
            document.setdefault("history", []).append({"at": _now(), "event": event})
        _upsert(connection, document)
    return document


def delete(doc_id: str) -> bool:
    with _LOCK, _connect() as connection:
        cursor = connection.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return cursor.rowcount > 0


def clear() -> None:
    with _LOCK, _connect() as connection:
        connection.execute("DELETE FROM documents")
