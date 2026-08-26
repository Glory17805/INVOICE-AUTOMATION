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

import pytest

from app import db, pipeline, store


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_llm: test may call a real LLM provider; excluded from the offline default.",
    )


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
    """
    scratch = tmp_path / "isolated-data" / "store.json"
    scratch.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(store, "STORE_PATH", scratch)
    monkeypatch.setattr(db, "STORE_PATH", scratch)
    db.forget()
    store._prepared.clear()
    yield
    db.forget()
    store._prepared.clear()
