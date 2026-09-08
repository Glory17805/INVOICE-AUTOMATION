"""The application's database: SQLite on a laptop, PostgreSQL in Azure.

The invoice queue, the user accounts and the audit trail share one database.
That is one thing to back up, one transaction boundary, and - on a laptop -
still no service to run.

Two engines, one schema
-----------------------
SQLite is what a developer gets by default and what the test suite runs
against: no server, a fresh database per test, 356 tests in two minutes.
PostgreSQL is what the deployment uses, chosen when DATABASE_URL is set.

The obvious objection is that testing on one engine and shipping on another
proves nothing. That is why the schema below is a single template rather than
two hand-maintained copies - a new table cannot be added to one engine and
forgotten in the other - and why the dialect differences are held to three
substitutions, listed in _DIALECTS. Everything else, including the upserts,
is syntax both engines already accept: `ON CONFLICT ... DO UPDATE SET
excluded.x` is Postgres syntax that SQLite adopted verbatim.

Why Postgres at all, when SQLite has been fine
----------------------------------------------
Every persistent disk offered by Azure App Service is SMB-backed, and SQLite's
WAL mode uses shared-memory mapping that SMB does not support. Turning WAL off
makes it technically work, and leaves a database holding a company's tax
records one dropped SMB connection away from corruption. singleton.py already
goes to considerable lengths to prevent a silently lost tax row; accepting that
same risk from the storage layer would be inconsistent.

Case-insensitive email
----------------------
Accounts are looked up with `WHERE email = ?` and rely entirely on the column
collation to match Priya@ against priya@. SQLite spells that COLLATE NOCASE;
Postgres spells it CITEXT. Using the extension rather than rewriting the four
lookups to lower(email) keeps the two engines behaviourally identical - which
is the whole basis for trusting a SQLite test run to say anything about
production - and keeps the UNIQUE constraint case-insensitive too.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from .config import STORE_PATH, ensure_dirs

# Serialises writers within this process. Both engines handle cross-process
# locking themselves, and the backend is single-instance by design anyway
# (see singleton.py), so this is belt-and-braces rather than the real defence.
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
    email         {email} NOT NULL UNIQUE,
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
    id      {autoinc},
    subject TEXT NOT NULL,     -- email and address together
    at      REAL NOT NULL      -- unix seconds
);
CREATE INDEX IF NOT EXISTS login_failures_subject ON login_failures(subject, at);

-- Who did what. A filing system gets asked this by auditors, so it is a table
-- rather than a log line that rotates away.
CREATE TABLE IF NOT EXISTS activity (
    id         {autoinc},
    at         TEXT NOT NULL,
    user_id    TEXT,
    user_email TEXT,
    action     TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS activity_at ON activity(at DESC);
"""

# The complete set of differences between the two engines. Keeping this list
# short is the point: every entry is a place where a SQLite test run proves
# slightly less about production than it appears to.
_DIALECTS = {
    "sqlite": {
        "autoinc": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "email": "TEXT COLLATE NOCASE",
        "preamble": "",
    },
    "postgres": {
        "autoinc": "BIGSERIAL PRIMARY KEY",
        "email": "CITEXT",
        "preamble": "CREATE EXTENSION IF NOT EXISTS citext;\n",
    },
}

# Columns added after the first release. CREATE TABLE IF NOT EXISTS does nothing
# to a table that already exists, so a new column has to be added explicitly or
# it appears only on machines that started fresh.
_ADDED_COLUMNS = [
    # Whether an administrator has let this account in. Existing rows default to
    # 1: everyone who already had an account keeps it.
    ("users", "approved", "INTEGER NOT NULL DEFAULT 1"),
]


def url() -> str:
    """The PostgreSQL connection string, or empty for SQLite.

    Read from the real environment rather than the .env file: a database URL
    carries a password, and Azure supplies it as an app setting. Nothing should
    be able to point production at a different database by editing a file that
    ships in the image.
    """
    return os.environ.get("DATABASE_URL", "").strip()


def dialect() -> str:
    return "postgres" if url() else "sqlite"


def schema_for(name: str) -> str:
    spec = _DIALECTS[name]
    return spec["preamble"] + SCHEMA.format(autoinc=spec["autoinc"], email=spec["email"])


# --------------------------------------------------------------------------- #
# Placeholder translation
# --------------------------------------------------------------------------- #

_PLACEHOLDER = re.compile(r"'[^']*'|(\?)|:([a-zA-Z_][a-zA-Z0-9_]*)")


def to_pyformat(sql: str) -> str:
    """Rewrite SQLite's placeholders as psycopg's.

    Both of sqlite3's styles are in use here - `?` with a tuple nearly
    everywhere, and `:name` with a dict in accounts.create_user - so both are
    translated: `?` becomes `%s`, `:name` becomes `%(name)s`. Handling the
    named form generally rather than rewriting that one statement means a
    query added later in either style cannot fail in Azure and nowhere else.

    Done here rather than by editing every query so the callers stay
    engine-agnostic and the test suite exercises the same SQL strings the
    deployment runs.

    Single-quoted literals are skipped, so a `?` or a `12:30` inside a string
    survives, and a literal `%` is doubled - psycopg reads an odd one as the
    start of a placeholder.
    """
    def replace(match: re.Match) -> str:
        if match.group(1):
            return "%s"
        if match.group(2):
            return f"%({match.group(2)})s"
        return match.group(0)

    return _PLACEHOLDER.sub(replace, sql.replace("%", "%%"))


class _PostgresConnection:
    """psycopg wearing the sqlite3 connection's shape.

    Only the handful of methods this codebase actually calls are forwarded
    deliberately - a wrapper that quietly passed everything through would let
    an engine-specific call slip in and only fail in Azure.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        # psycopg interpolates only when params is not None, so an empty tuple
        # must become None or a bare `%` in DDL is read as a placeholder.
        return self._raw.execute(to_pyformat(sql) if params else sql, params or None)

    def executescript(self, script: str) -> None:
        self._raw.execute(script)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


# Databases whose schema has been applied this run.
_ready: set[str] = set()


def path() -> Path:
    """The SQLite file, named after the JSON store it replaced.

    Deriving it from STORE_PATH keeps a single knob: tests point STORE_PATH at a
    temporary directory and get an isolated database for free.
    """
    return Path(STORE_PATH).with_suffix(".db")


def forget() -> None:
    """Drop the this-run schema cache. Tests call this between databases."""
    _ready.clear()


def _migrate(connection, name: str) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        if column not in _columns(connection, table, name):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _columns(connection, table: str, name: str) -> set[str]:
    if name == "sqlite":
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


@contextmanager
def connect():
    """A connection with the schema guaranteed present, committed on success."""
    name = dialect()
    connection = _connect_postgres() if name == "postgres" else _connect_sqlite()
    key = url() if name == "postgres" else str(path())
    try:
        if key not in _ready:
            connection.executescript(schema_for(name))
            _migrate(connection, name)
            _ready.add(key)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def journal_mode() -> str:
    """WAL on a local disk, DELETE on a network share.

    WAL is the better mode and the default: it lets a reader and a writer
    overlap instead of blocking, which is exactly what inbox polling does
    against a background read.

    It cannot be used on SMB. WAL coordinates readers and writers through a
    shared-memory index (`-shm`) backed by mmap, and SMB does not implement the
    shared memory it needs; the failure is not a clean error but a database
    that reads as corrupt to the next process to open it. Every persistent
    volume Azure offers a container is SMB-backed, so a deployment that keeps
    SQLite has to set GST_SQLITE_JOURNAL=DELETE.

    That mode is slower - each write takes an exclusive lock for the length of
    the transaction - which does not matter here: one process, one writer, and
    a queue measured in tens of documents.
    """
    configured = os.environ.get("GST_SQLITE_JOURNAL", "WAL").strip().upper()
    return configured if configured in ("WAL", "DELETE", "TRUNCATE", "PERSIST") else "WAL"


def _connect_sqlite():
    ensure_dirs()
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(file, timeout=15)
    connection.row_factory = sqlite3.Row

    mode = journal_mode()
    connection.execute(f"PRAGMA journal_mode={mode}")
    # NORMAL is safe under WAL, where a lost write costs the last transaction
    # and nothing structural. Without WAL it is not: on a network share, a
    # connection dropped between the write and the flush can leave the file
    # itself inconsistent. FULL costs an fsync per commit and buys back the
    # guarantee that a posted tax row is either fully written or not there.
    connection.execute(
        "PRAGMA synchronous=" + ("NORMAL" if mode == "WAL" else "FULL")
    )
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _connect_postgres():
    import psycopg
    from psycopg.rows import dict_row

    # Rows come back as dicts so `row["data"]` and `row.keys()` behave as they
    # do under sqlite3.Row, which is what every caller here already expects.
    return _PostgresConnection(
        psycopg.connect(url(), row_factory=dict_row, connect_timeout=15, autocommit=False)
    )
