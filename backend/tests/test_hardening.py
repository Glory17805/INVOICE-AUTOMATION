"""Tests for the operational properties, as opposed to the GST arithmetic.

Authentication, one-process-per-workbook, deferred reading, and the read cache.
These are the things that are fine until the day they are not, so each one is
pinned by the behaviour that would otherwise fail silently.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import pipeline, singleton, store, workbook
from app.config import IRA_INNOVATIONS
from app.models import DocStatus, DocumentType
from app.security import ApiKeyMiddleware

from .test_gst import invoice
from .test_workbook import PERIOD


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

def _guarded_app() -> FastAPI:
    """A minimal app behind the same middleware the real one uses."""
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/secret")
    def secret():
        return {"value": 42}

    return app


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("GST_API_KEY", "correct-horse-battery-staple")
    return TestClient(_guarded_app())


@pytest.fixture
def unguarded(monkeypatch):
    monkeypatch.setenv("GST_API_KEY", "")
    monkeypatch.setattr("app.security.api_key", lambda: "")
    return TestClient(_guarded_app())


def test_no_key_configured_leaves_the_api_open(unguarded):
    """Someone running both halves on their own machine is not asked to log in."""
    assert unguarded.get("/api/secret").status_code == 200


def test_a_request_without_a_key_is_refused(guarded):
    response = guarded.get("/api/secret")
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["detail"]


def test_a_request_with_the_wrong_key_is_refused(guarded):
    assert guarded.get("/api/secret", headers={"X-API-Key": "guess"}).status_code == 401


def test_a_request_with_the_right_key_is_allowed(guarded):
    response = guarded.get("/api/secret", headers={"X-API-Key": "correct-horse-battery-staple"})
    assert response.status_code == 200
    assert response.json() == {"value": 42}


def test_a_bearer_token_is_accepted_too(guarded):
    """So curl and Postman work without special-casing this API."""
    response = guarded.get(
        "/api/secret", headers={"Authorization": "Bearer correct-horse-battery-staple"}
    )
    assert response.status_code == 200


def test_health_is_reachable_without_a_key(guarded):
    """A liveness probe cannot be expected to hold credentials."""
    assert guarded.get("/api/health").status_code == 200


def test_a_preflight_is_not_blocked(guarded):
    """The browser sends OPTIONS before it will attach a custom header, so
    rejecting preflights would break every authenticated call from the page."""
    assert guarded.options("/api/secret").status_code != 401


def test_the_key_is_not_accepted_from_the_query_string(guarded):
    """Query strings end up in history, proxy logs and referrer headers."""
    assert guarded.get("/api/secret?key=correct-horse-battery-staple").status_code == 401


# --------------------------------------------------------------------------- #
# One process per data directory
# --------------------------------------------------------------------------- #

def test_a_second_backend_cannot_claim_the_same_data_directory(tmp_path):
    """Two processes appending to one workbook lose rows with no error at all,
    so the second one has to be stopped at the door."""
    lock = tmp_path / "backend.lock"
    singleton.acquire(lock)
    try:
        # Simulate a separate process: forget our handle without releasing the
        # kernel lock, then try to take it again the way a fresh start would.
        held, singleton._handle = singleton._handle, None
        with pytest.raises(singleton.AlreadyRunning) as caught:
            singleton.acquire(lock)
        assert "already using" in str(caught.value)
        singleton._handle = held
    finally:
        singleton.release()


def test_the_claim_is_released_and_can_be_retaken(tmp_path):
    """A restart must not need a stale lock file cleaned up by hand."""
    lock = tmp_path / "backend.lock"
    singleton.acquire(lock)
    singleton.release()
    singleton.acquire(lock)      # would raise if the first claim outlived it
    singleton.release()


def test_claiming_twice_from_one_process_is_harmless(tmp_path):
    """An app that reloads must not deadlock against itself."""
    lock = tmp_path / "backend.lock"
    singleton.acquire(lock)
    singleton.acquire(lock)
    singleton.release()


# --------------------------------------------------------------------------- #
# Staging: an upload answers before the reading happens
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_pipeline(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path / "workbook")
    # Force the offline reader: these tests are about sequencing, and should
    # not depend on a network round trip or spend a model call.
    monkeypatch.setattr(pipeline, "has_credentials", lambda: False)
    (tmp_path / "incoming").mkdir(parents=True, exist_ok=True)
    return tmp_path


# Label on one line, value on the next: the layout a PDF text layer produces,
# and the one the offline reader is written against.
INVOICE_TEXT = b"""TAX INVOICE
Invoice No.
STAGE-1
Invoice Date
15-May-2026
GSTIN
37AAKFI3341N1Z0
Taxable Value
1000.00
Total
1180.00
"""


def test_staging_registers_a_document_without_reading_it(isolated_pipeline):
    """The whole point of the split: the document exists before it is read."""
    staged = pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")

    assert len(staged) == 1
    assert staged[0]["status"] == DocStatus.NEW.value
    # Nothing has looked at the contents yet.
    assert "extracted" not in staged[0]
    assert "treatment" not in staged[0]


def test_reading_afterwards_moves_it_out_of_new(isolated_pipeline):
    staged = pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")
    pipeline.process_many([doc["id"] for doc in staged])

    after = store.get(staged[0]["id"])
    assert after["status"] != DocStatus.NEW.value
    assert after["extracted"]["invoice_number"] == "STAGE-1"


def test_capture_still_stages_and_reads_in_one_call(isolated_pipeline):
    """The synchronous path the watch folder and the tests use is unchanged."""
    records = pipeline.capture(INVOICE_TEXT, "invoice.txt", source="scan")

    assert len(records) == 1
    assert records[0]["status"] != DocStatus.NEW.value
    assert records[0]["extracted"]["invoice_number"] == "STAGE-1"


def test_a_document_that_cannot_be_read_is_marked_failed_not_lost(isolated_pipeline, monkeypatch):
    """process_many runs detached from any request. A traceback there would be
    seen by nobody, so the failure has to land on the document itself."""
    staged = pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")

    def explode(_doc_id):
        raise RuntimeError("the reader fell over")

    monkeypatch.setattr(pipeline, "process", explode)
    pipeline.process_many([doc["id"] for doc in staged])   # must not raise

    after = store.get(staged[0]["id"])
    assert after["status"] == DocStatus.FAILED.value
    assert "the reader fell over" in after["error"]


def test_an_unsupported_file_is_rejected_at_staging(isolated_pipeline):
    with pytest.raises(ValueError, match="unsupported file type"):
        pipeline.stage(b"whatever", "notes.docx", source="upload")


# --------------------------------------------------------------------------- #
# The read cache
# --------------------------------------------------------------------------- #

@pytest.fixture
def cached_workbook(tmp_path, monkeypatch):
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path)
    workbook._read_cache.clear()
    workbook.ensure_working_copy(PERIOD)
    yield
    workbook._read_cache.clear()


def _a_sale(number: str):
    return invoice(
        invoice_number=number, invoice_date="31-May-2026",
        supplier_gstin=IRA_INNOVATIONS.gstin, supplier_name="Ira Innovations",
        recipient_name="Prasuna Reddy", place_of_supply="Andhra Pradesh",
        taxable_value=1000, gst_rate_percent=18,
    )


def test_an_unchanged_workbook_is_only_read_once(cached_workbook):
    first = workbook.read_register(DocumentType.SALES, PERIOD)
    second = workbook.read_register(DocumentType.SALES, PERIOD)
    assert first is second, "the second read should have been served from cache"


def test_posting_a_row_invalidates_the_cache(cached_workbook):
    """A cache that could serve a register missing a row someone just posted
    would be worse than no cache at all."""
    before = workbook.read_register(DocumentType.SALES, PERIOD)
    assert before == []

    from app.gst import rules
    treatment = rules.apply_gst(_a_sale("CACHE-1"), IRA_INNOVATIONS)
    workbook.post_row(treatment, {
        "period": PERIOD, "invoice_number": "CACHE-1",
        "invoice_date_obj": workbook.parse_date("31-May-2026"),
        "hsn_sac": None, "quantity": None, "unit_rate": None,
    }, PERIOD)

    after = workbook.read_register(DocumentType.SALES, PERIOD)
    assert len(after) == 1
    assert after[0]["values"]["Invoice no"] == "CACHE-1"


def test_the_tax_position_is_recomputed_after_a_posting(cached_workbook):
    """The summary is cached on the same key space as the registers."""
    before = workbook.tax_payable_summary(PERIOD).output_tax["cgst"]

    from app.gst import rules
    treatment = rules.apply_gst(_a_sale("CACHE-2"), IRA_INNOVATIONS)
    workbook.post_row(treatment, {
        "period": PERIOD, "invoice_number": "CACHE-2",
        "invoice_date_obj": workbook.parse_date("31-May-2026"),
        "hsn_sac": None, "quantity": None, "unit_rate": None,
    }, PERIOD)

    after = workbook.tax_payable_summary(PERIOD).output_tax["cgst"]
    assert after == pytest.approx(before + 90.0)


# --------------------------------------------------------------------------- #
# The document store
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    store._prepared.clear()
    yield tmp_path
    store._prepared.clear()


def test_a_document_round_trips(isolated_store):
    added = store.add({"filename": "one.pdf", "status": "new"})
    fetched = store.get(added["id"])

    assert fetched["filename"] == "one.pdf"
    assert fetched["status"] == "new"
    # add() stamps the things every document is expected to carry.
    assert fetched["received_at"]
    assert fetched["history"][0]["event"] == "received"


def test_updates_merge_and_record_history(isolated_store):
    added = store.add({"filename": "one.pdf", "status": "new"})
    updated = store.update(added["id"], {"status": "ready", "period": "May-26"}, event="read")

    assert updated["status"] == "ready"
    assert updated["period"] == "May-26"
    assert updated["filename"] == "one.pdf", "an update must not drop untouched fields"
    assert [h["event"] for h in updated["history"]] == ["received", "read"]


def test_documents_come_back_newest_first(isolated_store):
    store.add({"filename": "older.pdf", "received_at": "2026-05-01T10:00:00+00:00"})
    store.add({"filename": "newer.pdf", "received_at": "2026-07-01T10:00:00+00:00"})

    assert [d["filename"] for d in store.all_documents()] == ["newer.pdf", "older.pdf"]


def test_operations_on_an_unknown_document_are_not_errors(isolated_store):
    assert store.get("nope") is None
    assert store.update("nope", {"status": "ready"}) is None
    assert store.delete("nope") is False


def test_delete_and_clear(isolated_store):
    first = store.add({"filename": "one.pdf"})
    store.add({"filename": "two.pdf"})

    assert store.delete(first["id"]) is True
    assert len(store.all_documents()) == 1

    store.clear()
    assert store.all_documents() == []


def test_an_existing_json_queue_is_imported_and_kept(isolated_store):
    """Nobody should lose their queue to a storage change - and if the import
    misreads something, the original file has to still be there."""
    legacy = isolated_store / "store.json"
    legacy.write_text(json.dumps({"documents": [
        {"id": "aaa", "filename": "carried.pdf", "status": "ready",
         "period": "May-26", "received_at": "2026-05-02T09:00:00+00:00"},
        {"id": "bbb", "filename": "also.pdf", "status": "posted",
         "period": "May-26", "received_at": "2026-05-03T09:00:00+00:00"},
    ]}), encoding="utf-8")

    documents = store.all_documents()

    assert [d["filename"] for d in documents] == ["also.pdf", "carried.pdf"]
    assert store.get("aaa")["status"] == "ready"
    # Kept, renamed - not deleted.
    assert not legacy.exists()
    assert (isolated_store / "store.json.imported").exists()


def test_the_import_runs_once_and_cannot_duplicate_a_queue(isolated_store):
    legacy = isolated_store / "store.json"
    legacy.write_text(json.dumps({"documents": [
        {"id": "aaa", "filename": "carried.pdf", "received_at": "2026-05-02T09:00:00+00:00"},
    ]}), encoding="utf-8")

    store.all_documents()
    store._prepared.clear()          # as though the process had restarted
    store.all_documents()

    assert len(store.all_documents()) == 1


def test_editing_the_file_underneath_is_noticed(cached_workbook):
    """The client keeps these sheets by hand. A row typed in Excel while the
    app is running must show up, not be masked by a cached read."""
    from openpyxl import load_workbook

    assert workbook.read_register(DocumentType.SALES, PERIOD) == []

    path = workbook.workbook_path(PERIOD)
    wb = load_workbook(path)
    ws = wb["GSTR-1"]
    ws["C8"], ws["E8"], ws["I8"], ws["J8"] = "TYPED-IN-EXCEL", "Someone", 0.18, 1000
    wb.save(path)
    wb.close()

    rows = workbook.read_register(DocumentType.SALES, PERIOD)
    assert [r["values"]["Invoice no"] for r in rows] == ["TYPED-IN-EXCEL"]
