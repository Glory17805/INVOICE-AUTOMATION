"""The application's single SQLite database.

The invoice queue, the user accounts and the audit trail share one file. That is
one thing to back up, one transaction boundary, and still no service to run.

Every table's schema lives here rather than in the module that uses it, so a
table is never created by whichever module happens to open the database first,
and the whole shape of the stored data can be read in one place.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from .config import STORE_PATH, ensure_dirs

# Serialises writers within this process. SQLite handles cross-process locking
# itself, but the backend is single-instance by design anyway (see singleton.py).
LOCK = RLock()

SCHEMA = """
-- The invoice queue. status and period are lifted out of the JSON blob because
-- they are what the inbox filters and groups by; everything else stays in it.
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

-- Accounts. Email is the login and is compared case-insensitively, because
-- nobody remembers whether they signed up as Priya@ or priya@.
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    password_hash TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- Only the hash of a session token is stored: a stolen database should not
-- hand over live sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Failed sign-ins. In a table rather than a process dictionary because the
-- backend restarts often, and a lockout that clears on restart is one an
-- attacker can clear by waiting for a deploy.
CREATE TABLE IF NOT EXISTS login_failures (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,     -- email and address together
    at      REAL NOT NULL      -- unix seconds
);
CREATE INDEX IF NOT EXISTS login_failures_subject ON login_failures(subject, at);

-- Who did what. A filing system gets asked this by auditors, so it is a table
-- rather than a log line that rotates away.
CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    user_id    TEXT,
    user_email TEXT,
    action     TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS activity_at ON activity(at DESC);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS does nothing
# to a table that already exists, so a new column has to be added explicitly or
# it appears only on machines that started fresh.
_ADDED_COLUMNS = [
    # Whether an administrator has let this account in. Existing rows default to
    # 1: everyone who already had an account keeps it.
    ("users", "approved", "INTEGER NOT NULL DEFAULT 1"),
]


def _migrate(connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# Databases whose schema has been applied this run.
_ready: set[str] = set()


def path() -> Path:
    """The database file, named after the JSON store it replaced.

    Deriving it from STORE_PATH keeps a single knob: tests point STORE_PATH at a
    temporary directory and get an isolated database for free.
    """
    return Path(STORE_PATH).with_suffix(".db")


def forget() -> None:
    """Drop the this-run schema cache. Tests call this between databases."""
    _ready.clear()


@contextmanager
def connect():
    """A connection with the schema guaranteed present, committed on success."""
    ensure_dirs()
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(file, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        # WAL lets a reader and a writer overlap instead of blocking, which is
        # what inbox polling does against a background read.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        if str(file) not in _ready:
            connection.executescript(SCHEMA)
            _migrate(connection)
            _ready.add(str(file))
        yield connection
        connection.commit()
    finally:
        connection.close()
