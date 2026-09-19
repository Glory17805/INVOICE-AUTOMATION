"""Tests for the operational properties, as opposed to the GST arithmetic.

Authentication, one-process-per-workbook, deferred reading, and the read cache.
These are the things that are fine until the day they are not, so each one is
pinned by the behaviour that would otherwise fail silently.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import (
    accounts,
    appsettings,
    auth,
    db,
    pipeline,
    runtime,
    singleton,
    store,
    workbook,
)
from app.config import IRA_INNOVATIONS
from app.models import DocStatus, DocumentType

from .test_gst import invoice
from .test_workbook import PERIOD

# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr("app.db.STORE_PATH", tmp_path / "store.json")
    db.forget()
    store._prepared.clear()
    auth.reset_throttle()
    yield tmp_path
    db.forget()
    store._prepared.clear()


def test_a_password_verifies_only_against_itself():
    stored = accounts.hash_password("a decent long password")
    assert accounts.verify_password("a decent long password", stored)
    assert not accounts.verify_password("a decent long passwore", stored)


def test_the_same_password_hashes_differently_each_time():
    """Per-password salt: two people with the same password must not be
    visibly identical in the database."""
    assert accounts.hash_password("shared password") != accounts.hash_password("shared password")


def test_a_short_password_is_refused(isolated_accounts):
    with pytest.raises(accounts.AccountError, match="10 characters"):
        accounts.create_user("a@b.com", "A", "short")


def test_creating_and_finding_a_user(isolated_accounts):
    created = accounts.create_user("Priya@Example.com", "Priya", "a decent long password")
    assert created["role"] == "user"
    assert created["is_active"] is True
    # Email is the login, and nobody remembers their own capitalisation.
    assert accounts.find_by_email("priya@example.com")["id"] == created["id"]
    # The hash never leaves the module.
    assert "password_hash" not in created


def test_a_duplicate_email_is_refused(isolated_accounts):
    accounts.create_user("a@b.com", "A", "a decent long password")
    with pytest.raises(accounts.AccountError, match="already exists"):
        accounts.create_user("A@B.com", "Another", "a decent long password")


def test_authentication_accepts_the_right_password(isolated_accounts):
    accounts.create_user("a@b.com", "A", "a decent long password")
    assert accounts.authenticate("a@b.com", "a decent long password")["email"] == "a@b.com"


def test_a_wrong_password_and_an_unknown_account_are_indistinguishable(isolated_accounts):
    """Different messages would turn this into a way to discover who has an
    account here."""
    accounts.create_user("a@b.com", "A", "a decent long password")

    with pytest.raises(accounts.AccountError) as wrong:
        accounts.authenticate("a@b.com", "not the password")
    with pytest.raises(accounts.AccountError) as unknown:
        accounts.authenticate("nobody@b.com", "not the password")

    assert str(wrong.value) == str(unknown.value)


def test_a_disabled_account_cannot_sign_in(isolated_accounts):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    accounts.create_user("boss@b.com", "Boss", "a decent long password", role="admin")
    accounts.update_user(user["id"], is_active=False)

    with pytest.raises(accounts.AccountError, match="disabled"):
        accounts.authenticate("a@b.com", "a decent long password")


# --------------------------------------------------------------------------- #
# Self-signup
#
# There is no approval gate. An account either works or was never created:
# holding credentials that silently do nothing is indistinguishable, to the
# person holding them, from a system that is broken.
# --------------------------------------------------------------------------- #

def test_a_new_account_can_sign_in_immediately(isolated_accounts):
    accounts.create_user("new@b.com", "New", "a decent long password")
    assert accounts.authenticate("new@b.com", "a decent long password")["email"] == "new@b.com"


def test_an_account_left_waiting_by_the_old_gate_can_now_sign_in(isolated_accounts):
    """Rows written before the gate was removed must not be stranded.

    Nothing approves them any more, so without the migration in `db._migrate`
    they would be accounts that exist, hold the right password, and refuse to
    sign in for ever, with nothing in the interface able to repair them.
    """
    person = accounts.create_user("old@b.com", "Old", "a decent long password")
    with db.LOCK, db.connect() as c:
        c.execute("UPDATE users SET approved = 0 WHERE id = ?", (person["id"],))

    db.forget()   # force the schema/migration pass to run again
    with db.LOCK, db.connect() as c:
        pass

    assert accounts.authenticate("old@b.com", "a decent long password")["id"] == person["id"]


def test_being_switched_off_still_refuses(isolated_accounts):
    """Removing the approval gate must not remove the one that matters: an
    administrator disabling an account is a decision about a real account."""
    disabled = accounts.create_user("off@b.com", "Off", "a decent long password")
    accounts.update_user(disabled["id"], is_active=False)
    with pytest.raises(accounts.AccountError, match="disabled"):
        accounts.authenticate("off@b.com", "a decent long password")


def test_a_disabled_admin_is_not_a_way_back_in(isolated_accounts):
    """Demoting the only usable admin must not be allowed just because another
    admin row exists that is switched off - that account cannot sign in."""
    boss = accounts.create_user("boss@b.com", "Boss", "a decent long password", role="admin")
    ghost = accounts.create_user("ghost@b.com", "Ghost", "a decent long password", role="admin")
    accounts.update_user(ghost["id"], is_active=False)

    with pytest.raises(accounts.AccountError, match="only active administrator"):
        accounts.update_user(boss["id"], role="user")


def test_accounts_are_created_ready_to_use(isolated_accounts):
    created = accounts.create_user("a@b.com", "A", "a decent long password")
    assert created["approved"] is True
    assert created["awaiting_approval"] is False


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def test_a_session_token_resolves_to_its_user(isolated_accounts):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    assert accounts.user_for_token(token)["id"] == user["id"]


def test_the_raw_token_is_never_stored(isolated_accounts):
    """A copy of the database must not be a set of live sessions."""
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    with db.connect() as c:
        stored = [row["token_hash"] for row in c.execute("SELECT token_hash FROM sessions")]
    assert token not in stored


def test_logging_out_ends_the_session(isolated_accounts):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    accounts.close_session(token)
    assert accounts.user_for_token(token) is None


def test_an_unknown_token_resolves_to_nobody(isolated_accounts):
    assert accounts.user_for_token("not-a-real-token") is None
    assert accounts.user_for_token("") is None


def test_changing_a_password_ends_every_session(isolated_accounts):
    """The point of changing it after a scare."""
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    accounts.set_password(user["id"], "a different long password")
    assert accounts.user_for_token(token) is None


def test_disabling_an_account_ends_its_sessions(isolated_accounts):
    accounts.create_user("boss@b.com", "Boss", "a decent long password", role="admin")
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    accounts.update_user(user["id"], is_active=False)
    assert accounts.user_for_token(token) is None


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #

def test_a_reset_token_sets_a_new_password(isolated_accounts):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token, _ = accounts.begin_reset("a@b.com")
    accounts.complete_reset(token, "a brand new long password")
    assert accounts.authenticate("a@b.com", "a brand new long password")["id"] == user["id"]


def test_a_reset_token_works_only_once(isolated_accounts):
    accounts.create_user("a@b.com", "A", "a decent long password")
    token, _ = accounts.begin_reset("a@b.com")
    accounts.complete_reset(token, "a brand new long password")
    with pytest.raises(accounts.AccountError, match="expired or has already been used"):
        accounts.complete_reset(token, "yet another long password")


def test_resetting_an_unknown_address_yields_nothing(isolated_accounts):
    """The route must answer identically either way; this is the half that
    makes that possible without inventing a token."""
    assert accounts.begin_reset("nobody@example.com") is None


def test_a_reset_ends_existing_sessions(isolated_accounts):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    reset_token, _ = accounts.begin_reset("a@b.com")
    accounts.complete_reset(reset_token, "a brand new long password")
    assert accounts.user_for_token(token) is None


# --------------------------------------------------------------------------- #
# Roles, and not locking everyone out
# --------------------------------------------------------------------------- #

def test_the_last_administrator_cannot_be_demoted(isolated_accounts):
    boss = accounts.create_user("boss@b.com", "Boss", "a decent long password", role="admin")
    accounts.create_user("a@b.com", "A", "a decent long password")
    with pytest.raises(accounts.AccountError, match="only active administrator"):
        accounts.update_user(boss["id"], role="user")


def test_the_last_administrator_cannot_be_disabled(isolated_accounts):
    boss = accounts.create_user("boss@b.com", "Boss", "a decent long password", role="admin")
    with pytest.raises(accounts.AccountError, match="only active administrator"):
        accounts.update_user(boss["id"], is_active=False)


def test_the_last_administrator_cannot_be_deleted(isolated_accounts):
    boss = accounts.create_user("boss@b.com", "Boss", "a decent long password", role="admin")
    with pytest.raises(accounts.AccountError, match="only administrator"):
        accounts.delete_user(boss["id"])


def test_an_administrator_can_be_demoted_once_there_is_another(isolated_accounts):
    first = accounts.create_user("one@b.com", "One", "a decent long password", role="admin")
    accounts.create_user("two@b.com", "Two", "a decent long password", role="admin")
    assert accounts.update_user(first["id"], role="user")["role"] == "user"


# --------------------------------------------------------------------------- #
# Route guards
# --------------------------------------------------------------------------- #

def _guarded_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/open")
    def open_route():
        return {"ok": True}

    @app.get("/api/private")
    def private(user: dict = Depends(auth.require_user)):
        return {"as": user["email"]}

    @app.get("/api/admin-only")
    def admin_only(user: dict = Depends(auth.require_admin)):
        return {"as": user["email"]}

    return app


@pytest.fixture
def client(isolated_accounts):
    return TestClient(_guarded_app())


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_an_anonymous_request_is_refused(client):
    assert client.get("/api/private").status_code == 401


def test_a_signed_in_user_is_allowed(client):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    response = client.get("/api/private", headers=_bearer(token))
    assert response.status_code == 200
    assert response.json() == {"as": "a@b.com"}


def test_a_plain_user_is_refused_an_admin_route(client):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    response = client.get("/api/admin-only", headers=_bearer(token))
    assert response.status_code == 403
    assert "administrator" in response.json()["detail"]


def test_an_administrator_is_allowed(client):
    boss = accounts.create_user("boss@b.com", "Boss", "a decent long password", role="admin")
    token = accounts.open_session(boss["id"])
    assert client.get("/api/admin-only", headers=_bearer(token)).status_code == 200


def test_a_stale_token_is_refused(client):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    token = accounts.open_session(user["id"])
    accounts.close_session(token)
    assert client.get("/api/private", headers=_bearer(token)).status_code == 401


def test_the_service_key_authenticates_as_a_service(client, monkeypatch):
    """Scripts need a way in that is not a person's password - and the audit
    trail has to be able to tell the difference."""
    monkeypatch.setattr("app.auth.api_key", lambda: "a-service-key")
    response = client.get("/api/admin-only", headers=_bearer("a-service-key"))
    assert response.status_code == 200
    assert response.json() == {"as": "service-key"}


def test_no_service_key_configured_means_that_value_is_not_special(client, monkeypatch):
    monkeypatch.setattr("app.auth.api_key", lambda: "")
    assert client.get("/api/private", headers=_bearer("")).status_code == 401


# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #

def test_repeated_failures_start_imposing_a_wait(isolated_accounts):
    """A secret on an open port invites guessing; nothing else slows it down."""
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))

    assert auth.retry_after("a@b.com", request) == 0
    for _ in range(5):
        auth.note_failure("a@b.com", request)
    assert auth.retry_after("a@b.com", request) > 0


def test_a_successful_login_clears_the_count(isolated_accounts):
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))
    for _ in range(5):
        auth.note_failure("a@b.com", request)
    auth.clear_failures("a@b.com", request)
    assert auth.retry_after("a@b.com", request) == 0


def test_throttling_is_per_account_not_global(isolated_accounts):
    """One person fat-fingering their password must not lock out a colleague."""
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))
    for _ in range(6):
        auth.note_failure("victim@b.com", request)
    assert auth.retry_after("victim@b.com", request) > 0
    assert auth.retry_after("someone-else@b.com", request) == 0


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #

def test_actions_are_recorded_against_the_person(isolated_accounts):
    user = accounts.create_user("a@b.com", "A", "a decent long password")
    accounts.record(user, "posted", {"document": "abc123"})

    entry = accounts.activity(10)[0]
    assert entry["action"] == "posted"
    assert entry["user_email"] == "a@b.com"
    assert "abc123" in entry["detail"]


def test_a_broken_audit_write_never_breaks_the_action(isolated_accounts, monkeypatch):
    """Logging that something happened must not stop it happening."""
    monkeypatch.setattr(db, "connect", lambda: (_ for _ in ()).throw(RuntimeError("disk gone")))
    accounts.record({"id": "x", "email": "a@b.com"}, "posted")   # must not raise


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
    monkeypatch.setattr("app.db.STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path / "workbook")
    # Force the offline reader: these tests are about sequencing, and should
    # not depend on a network round trip or spend a model call.
    monkeypatch.setattr(pipeline, "has_credentials", lambda: False)
    db.forget()
    store._prepared.clear()
    (tmp_path / "incoming").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    db.forget()
    store._prepared.clear()


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
# Not accepting the same invoice twice
#
# A real queue reached 337 documents holding 19 distinct invoices, because
# nothing objected to the same file being uploaded again. The register check
# kept the workbook clean, but only at the very last step - by which point
# somebody had 273 redundant rows to work through.
# --------------------------------------------------------------------------- #

def test_the_same_file_twice_is_refused(isolated_pipeline):
    pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")

    with pytest.raises(pipeline.DuplicateUpload) as caught:
        pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")

    assert "already uploaded" in str(caught.value)
    assert len(caught.value.existing) == 1
    assert len(store.all_documents()) == 1, "the second upload must not create anything"


def test_the_same_bytes_under_a_different_name_are_still_refused(isolated_pipeline):
    """Saving the file again from a mail client renames it; it is the same
    invoice, and the name is the least reliable thing about it."""
    pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")
    with pytest.raises(pipeline.DuplicateUpload):
        pipeline.stage(INVOICE_TEXT, "invoice (1).txt", source="upload")


def test_a_genuinely_different_file_is_accepted(isolated_pipeline):
    pipeline.stage(INVOICE_TEXT, "one.txt", source="upload")
    other = INVOICE_TEXT.replace(b"STAGE-1", b"STAGE-2")
    assert len(pipeline.stage(other, "two.txt", source="upload")) == 1
    assert len(store.all_documents()) == 2


def test_a_duplicate_can_be_insisted_on(isolated_pipeline):
    """Refusing outright would be wrong: sometimes you do mean it."""
    pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")
    again = pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload", allow_duplicate=True)
    assert len(again) == 1
    assert len(store.all_documents()) == 2


def test_the_check_costs_nothing_before_the_file_is_stored(isolated_pipeline):
    """The point of checking at the door: a repeated print run must not cost a
    model call per invoice inside it before anyone notices."""
    pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")
    before = list((isolated_pipeline / "incoming").iterdir())

    with pytest.raises(pipeline.DuplicateUpload):
        pipeline.stage(INVOICE_TEXT, "invoice.txt", source="upload")

    assert list((isolated_pipeline / "incoming").iterdir()) == before


def test_the_same_invoice_in_a_different_file_is_flagged_after_reading(isolated_pipeline):
    """The hash cannot see this one: emailed once, scanned later, same invoice.
    A warning rather than a blocker - two queue entries are not yet a filing
    error, but nobody should have to spot it themselves."""
    pipeline.capture(INVOICE_TEXT, "emailed.txt", source="email")

    # Same invoice number, different bytes - a trailing line is enough.
    rescan = INVOICE_TEXT + b"\nScanned copy\n"
    second = pipeline.capture(rescan, "scanned.txt", source="scan")[0]

    codes = {issue["code"] for issue in second["issues"]}
    assert "duplicate_in_queue" in codes
    warning = next(i for i in second["issues"] if i["code"] == "duplicate_in_queue")
    assert warning["severity"] == "warning", "it must not block posting on its own"
    assert "emailed.txt" in warning["message"]


def test_a_lone_invoice_is_not_flagged_as_its_own_duplicate(isolated_pipeline):
    only = pipeline.capture(INVOICE_TEXT, "invoice.txt", source="upload")[0]
    assert "duplicate_in_queue" not in {i["code"] for i in only["issues"]}


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
    before = Decimal(workbook.tax_payable_summary(PERIOD).output_tax["cgst"])

    from app.gst import rules
    treatment = rules.apply_gst(_a_sale("CACHE-2"), IRA_INNOVATIONS)
    workbook.post_row(treatment, {
        "period": PERIOD, "invoice_number": "CACHE-2",
        "invoice_date_obj": workbook.parse_date("31-May-2026"),
        "hsn_sac": None, "quantity": None, "unit_rate": None,
    }, PERIOD)

    after = Decimal(workbook.tax_payable_summary(PERIOD).output_tax["cgst"])
    # Exact, not approximate: these are decimal strings now, so there is no
    # float error left to tolerate (gap D2).
    assert after == before + Decimal("90.00")


# --------------------------------------------------------------------------- #
# The document store
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr("app.db.STORE_PATH", tmp_path / "store.json")
    db.forget()
    store._prepared.clear()
    yield tmp_path
    db.forget()
    store._prepared.clear()


# --------------------------------------------------------------------------- #
# What the reader last did, and when to stop saying it
# --------------------------------------------------------------------------- #

def test_the_last_read_is_reported_back(isolated_store):
    runtime.record_read("gemini", True, "gemini", None)
    seen = runtime.last_read("gemini", True)
    assert seen["reader"] == "gemini"
    assert seen["at"]


def test_nothing_is_reported_before_anything_has_been_read(isolated_store):
    assert runtime.last_read("gemini", True) is None


def test_adding_a_key_retires_the_note_that_said_there_was_none(isolated_store):
    """The bug this exists for: a screen saying "your invoices may go to Gemini"
    and "no Gemini key is configured" at the same time. The second sentence was
    a true statement about a read that happened before the key was added."""
    runtime.record_read("gemini", False, "heuristic", "No GEMINI_API_KEY configured")

    # Same provider, but credentials now present: the note is about the past.
    assert runtime.last_read("gemini", True) is None
    # Unchanged configuration still reports it.
    assert runtime.last_read("gemini", False)["note"] == "No GEMINI_API_KEY configured"


def test_switching_provider_retires_the_note(isolated_store):
    runtime.record_read("claude", True, "heuristic", "credit balance is too low")
    assert runtime.last_read("gemini", True) is None
    assert runtime.last_read("claude", True)["reader"] == "heuristic"


def test_removing_a_key_retires_the_note_too(isolated_store):
    runtime.record_read("gemini", True, "gemini", None)
    assert runtime.last_read("gemini", False) is None


def test_recording_a_read_never_raises(isolated_store, monkeypatch):
    """Reporting what the reader did must not be able to break the reading."""
    monkeypatch.setattr(db, "connect", lambda: (_ for _ in ()).throw(RuntimeError("disk gone")))
    runtime.record_read("gemini", True, "gemini", None)      # must not raise
    assert runtime.last_read("gemini", True) is None         # nor must reading it


def test_signup_mode_defaults_to_open(isolated_store):
    """Open by default, because the alternative is a fresh install nobody can
    get into. Closed is the setting to reach for once the people who need
    accounts have them."""
    assert appsettings.all_settings()["signup_mode"] == "open"


def test_signup_mode_accepts_only_the_two_policies(isolated_store):
    for mode in ("open", "closed"):
        assert appsettings.update({"signup_mode": mode})["signup_mode"] == mode
    with pytest.raises(ValueError, match="Signup mode must be one of"):
        appsettings.update({"signup_mode": "everyone-welcome"})


def test_the_retired_approval_mode_reads_as_open(isolated_store):
    """A database written before the gate was removed still holds "approval".

    Refusing it would leave the settings screen unable to load, so an
    administrator could not even change it - a stored value the validator
    rejects locks the door it is standing in front of.
    """
    assert appsettings.update({"signup_mode": "approval"})["signup_mode"] == "open"
    assert appsettings.all_settings()["signup_mode"] == "open"


def test_an_unknown_setting_is_refused_rather_than_stored(isolated_store):
    """A settings table that accepts anything becomes a junk drawer."""
    with pytest.raises(ValueError, match="Unknown setting"):
        appsettings.update({"make_it_faster": True})


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
