"""Shared test setup.

The suite must not call an LLM. Before a working provider key existed this was
true by accident - the configured Anthropic key had no credit, so every read
failed instantly and fell back to the offline reader. The moment a working
Gemini key landed in .env, the same tests started making real API calls: one
of them captures an 18-invoice print run, which is 18 requests against a
free-tier quota and several minutes of wall clock, on every run.

So the offline reader is forced here for the whole suite. A test that genuinely
wants a provider either patches the reader itself (see test_providers.py) or
opts in with @pytest.mark.live_llm.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote

import pytest

from app import db, pipeline, store, workbook
from app.config import source_workbook


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_llm: test may call a real LLM provider; excluded from the offline default.",
    )


@pytest.fixture(autouse=True)
def skip_without_master_workbook(monkeypatch):
    """Skip the workbook tests when the client's workbook is not on this machine.

    test_workbook.py is deliberate about running against a throwaway copy of
    the real file rather than a fixture: the assertions are about that sheet's
    own formula idioms and number formats, and a fixture we wrote would only
    confirm it agrees with itself.

    The cost is that those tests cannot run anywhere the file is absent, and it
    must stay absent from the repository - it is a client's filing history.
    On a fresh clone this surfaced as 25 setup errors and a permanently red
    CI, which is worse than useless: a build that is always red reports
    nothing when it goes red for a real reason.

    So the one condition is converted into a skip, narrowly. Only calls that
    genuinely need the source file are intercepted, so a test failing for any
    other reason still fails.
    """
    if source_workbook().exists():
        return

    reason = (
        f"needs the master workbook at {source_workbook()}, which is a client's "
        "filing history and is deliberately not in the repository"
    )

    def skip(*_args, **_kwargs):
        pytest.skip(reason)

    monkeypatch.setattr(workbook, "ensure_working_copy", skip)
    monkeypatch.setattr(workbook, "master_period", skip)


@pytest.fixture(autouse=True)
def offline_reader(request, monkeypatch):
    """Force the offline reader unless a test opts out.

    Patched on `pipeline`, not `config`: pipeline imports the name directly, so
    patching the config module would leave the bound reference untouched and
    the suite would quietly keep calling the network.
    """
    if request.node.get_closest_marker("live_llm"):
        return
    monkeypatch.setattr(pipeline, "has_credentials", lambda: False)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """No test may touch the real database.

    Several tests pointed `store.STORE_PATH` at a temporary file and believed
    that isolated them. It stopped being true when the queue moved to SQLite:
    the database path is derived in `db`, so those tests kept reading and
    writing the live one. They had been quietly adding documents to a running
    deployment's queue for as long as that was the case, and the evidence was a
    real queue holding 337 documents for 19 distinct invoices.

    Isolation belongs here rather than in each test, because getting it right
    per-test is exactly what failed.

    DATABASE_URL is deleted for the same reason, one engine later. It outranks
    STORE_PATH entirely - when it is set, `db.path()` is never consulted - so
    patching the path above would isolate nothing at all, and a developer with
    the deployment's URL exported in their shell would point the whole suite at
    production. That is the 337-document incident again with a worse blast
    radius, so the variable is removed rather than trusted.

    To actually exercise Postgres, set GST_TEST_DATABASE_URL instead: each test
    then gets its own schema, created and dropped around it.
    """
    scratch = tmp_path / "isolated-data" / "store.json"
    scratch.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(store, "STORE_PATH", scratch)
    monkeypatch.setattr(db, "STORE_PATH", scratch)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # Posting now takes a backup first, and backup_dir() is derived from
    # config.DATA_DIR - which the two lines above do not touch. Left alone,
    # every test that posts a document would write a real archive into the
    # real backups directory, which is the 337-document mistake wearing a
    # different hat. Pointed at the same scratch space so the backup code is
    # still genuinely exercised rather than stubbed out.
    monkeypatch.setenv("GST_BACKUP_DIR", str(tmp_path / "isolated-backups"))

    schema = _postgres_schema(monkeypatch) if os.environ.get("GST_TEST_DATABASE_URL") else None

    db.forget()
    store._prepared.clear()
    yield
    db.forget()
    store._prepared.clear()
    if schema:
        _drop_schema(schema)


def _postgres_schema(monkeypatch) -> str:
    """Give this one test a private schema on the test server.

    A schema rather than a database because creating a database per test costs
    a second each; a schema costs milliseconds and isolates just as completely
    once search_path points at it alone.
    """
    import psycopg

    base = os.environ["GST_TEST_DATABASE_URL"]
    name = f"t{uuid.uuid4().hex[:16]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{name}"')
        # citext must live somewhere every test schema can see. An extension is
        # created into whichever schema heads search_path, so left to itself it
        # would land in the throwaway schema and vanish with it; and because
        # CREATE EXTENSION IF NOT EXISTS is database-wide rather than
        # per-schema, the second test would then find it "already existing"
        # somewhere it could no longer reach. Pinning it to public once, and
        # putting public at the tail of search_path below, is also exactly the
        # arrangement production gets.
        connection.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")

    separator = "&" if "?" in base else "?"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"{base}{separator}options=" + quote(f"-csearch_path={name},public"),
    )
    return name


def _drop_schema(name: str) -> None:
    import psycopg

    with psycopg.connect(os.environ["GST_TEST_DATABASE_URL"], autocommit=True) as connection:
        connection.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
