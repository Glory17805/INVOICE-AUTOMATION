"""Supplier history: what previous invoices say about a new one.

The idea is borrowed from a project that used scikit-learn's TF-IDF as an
offline retrieval store. The adaptation is the interesting part, and these
tests pin both halves of it: that the matching works on the name variations a
reader actually produces, and that none of it can reach the reader.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import history, store


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    scratch = tmp_path / "data" / "store.json"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "STORE_PATH", scratch)
    monkeypatch.setattr("app.db.STORE_PATH", scratch)
    from app import db
    db.forget()
    store._prepared.clear()
    yield tmp_path
    db.forget()
    store._prepared.clear()


def posted(name, gstin, rate="0.18", doc_type="purchase", place="Odisha", when="2026-05-01"):
    """A document as it looks once it has been confirmed into the workbook."""
    return {
        "id": store.new_id(),
        "filename": f"{name}.pdf",
        "status": "posted",
        "received_at": f"{when}T10:00:00+00:00",
        "treatment": {
            "counterparty_name": name,
            "counterparty_gstin": gstin,
            "rate": rate,
            "document_type": doc_type,
            "place_of_supply_name": place,
        },
    }


# --------------------------------------------------------------------------- #
# The retrieval itself
# --------------------------------------------------------------------------- #

def test_company_boilerplate_is_stripped_before_comparing():
    """"Private Limited" identifies nobody, and leaving it in makes every
    company look faintly like every other one."""
    assert history.normalise("Waymiro Private Limited") == "waymiro"
    assert history.normalise("WAYMIRO PVT. LTD.") == "waymiro"
    assert history.normalise("  The  Waymiro  Co.  ") == "waymiro"


def test_normalising_an_empty_name_is_not_an_error():
    assert history.normalise(None) == ""
    assert history.normalise("") == ""


def similarity(a: str, b: str) -> float:
    return history.cosine(history._vector(history.trigrams(history.normalise(a))),
                          history._vector(history.trigrams(history.normalise(b))))


def test_cosine_is_one_for_identical_text_and_zero_for_nothing_shared():
    assert similarity("waymiro", "waymiro") == pytest.approx(1.0)
    assert history.cosine(history._vector(history.trigrams("waymiro")), {}) == 0.0
    assert similarity("Swiggy", "Porter") == 0.0


@pytest.mark.parametrize("stored,incoming", [
    ("Waymiro Private Limited", "Waymlro Private Limited"),   # i misread as l
    ("Prasuna Reddy", "Prasuna Reddi"),
    ("Porter", "Portor"),
    ("SBI GENERAL INSURANCE COMPANY LTD", "SBI General Insurance Co."),
])
def test_a_single_character_misread_still_matches(stored, incoming):
    """The whole point of matching on trigrams rather than equality.

    This is also where the borrowed technique needed changing: with the IDF
    term included, "Waymiro" against "Waymlro" scored 0.46 and was missed. IDF
    suppresses the shared trigrams that carry the signal when the strings are
    this short.
    """
    assert similarity(stored, incoming) >= history.NAME_MATCH_THRESHOLD


@pytest.mark.parametrize("a,b", [
    ("Waymiro Private Limited", "Wintech Solutions"),
    ("SBI General Insurance", "SBI Cards and Payments"),      # same prefix, different company
    ("Swiggy", "Porter"),
])
def test_different_companies_do_not_match(a, b):
    assert similarity(a, b) < history.NAME_MATCH_THRESHOLD


def test_a_name_variant_matches_and_an_unrelated_name_does_not(isolated):
    for _ in range(3):
        store.add(posted("SBI GENERAL INSURANCE COMPANY LTD", "37AAMCS8857L1ZB"))

    profiles = history.build()
    assert history.match("SBI General Insurance Co.", None, profiles) is not None
    assert history.match("Swiggy", None, profiles) is None


def test_a_gstin_matches_exactly_regardless_of_the_name(isolated):
    """The name on an invoice varies; the GSTIN is the identity."""
    for _ in range(3):
        store.add(posted("WAYMIRO PRIVATE LIMITED", "21AADCW9393G1Z3"))

    found = history.match("something else entirely", "21aadcw9393g1z3")
    assert found is not None
    assert found.usual_gstin == "21AADCW9393G1Z3"


# --------------------------------------------------------------------------- #
# What a profile learns
# --------------------------------------------------------------------------- #

def test_only_posted_documents_teach_anything(isolated):
    """A queued document is a guess until someone confirms it. Learning from
    unconfirmed reads would let one misread invoice train the system to expect
    the same mistake."""
    for _ in range(3):
        store.add(posted("Waymiro", "21AADCW9393G1Z3"))
    guess = posted("Waymiro", "21WRONGWRONG1Z3")
    guess["status"] = "needs_review"
    store.add(guess)

    profile = history.match("Waymiro", "21AADCW9393G1Z3")
    assert profile.invoice_count == 3
    assert "21WRONGWRONG1Z3" not in profile.gstins


def test_a_profile_records_what_is_usual(isolated):
    for _ in range(4):
        store.add(posted("Porter", "36AAGCR8772D1Z3", rate="0.05", doc_type="rcm"))

    profile = history.match("Porter", "36AAGCR8772D1Z3")
    assert profile.invoice_count == 4
    assert profile.usual_rate == Decimal("0.05")
    assert profile.as_dict()["registers"] == ["rcm"]
    assert profile.as_dict()["last_seen"]


# --------------------------------------------------------------------------- #
# The warnings, and their restraint
# --------------------------------------------------------------------------- #

def test_a_changed_gstin_is_flagged(isolated):
    for _ in range(3):
        store.add(posted("Waymiro", "21AADCW9393G1Z3"))

    codes = dict(history.anomalies("Waymiro", "21AADCW9393G1Z4", Decimal("0.18")))
    assert "supplier_gstin_differs" in codes
    assert "21AADCW9393G1Z3" in codes["supplier_gstin_differs"]


def test_a_missing_gstin_is_flagged_when_we_know_it(isolated):
    for _ in range(3):
        store.add(posted("Waymiro", "21AADCW9393G1Z3"))

    codes = dict(history.anomalies("Waymiro", None, Decimal("0.18")))
    assert "supplier_gstin_missing_but_known" in codes


def test_an_unusual_rate_is_flagged_for_a_consistent_supplier(isolated):
    for _ in range(3):
        store.add(posted("Porter", "36AAGCR8772D1Z3", rate="0.05"))

    codes = dict(history.anomalies("Porter", "36AAGCR8772D1Z3", Decimal("0.18")))
    assert "unusual_rate_for_supplier" in codes


def test_a_supplier_who_already_bills_several_rates_is_not_flagged(isolated):
    """Restraint: a supplier who has used 5% and 18% tells us nothing by using
    12%. Warning on that would train people to ignore warnings."""
    store.add(posted("Mixed Traders", "37AAKFI3341N1Z0", rate="0.05"))
    store.add(posted("Mixed Traders", "37AAKFI3341N1Z0", rate="0.18"))
    store.add(posted("Mixed Traders", "37AAKFI3341N1Z0", rate="0.18"))

    codes = dict(history.anomalies("Mixed Traders", "37AAKFI3341N1Z0", Decimal("0.12")))
    assert "unusual_rate_for_supplier" not in codes


def test_nothing_is_flagged_below_the_history_threshold(isolated):
    """One previous invoice is an anecdote."""
    store.add(posted("Waymiro", "21AADCW9393G1Z3"))
    assert history.anomalies("Waymiro", "21AADCW9393G1Z4", Decimal("0.18")) == []


def test_a_misread_name_with_a_wrong_gstin_is_still_caught(isolated):
    """The case worth catching most, and the one that slipped through before
    the matcher was fixed: both identifying fields are wrong at once, so exact
    lookup on either finds nothing."""
    for _ in range(4):
        store.add(posted("Waymiro Private Limited", "21AADCW9393G1Z3"))

    codes = dict(history.anomalies("Waymlro Pvt Ltd", "21AADCW9393G1Z4", Decimal("0.18")))
    assert "supplier_gstin_differs" in codes


def test_an_unknown_supplier_produces_no_noise(isolated):
    for _ in range(3):
        store.add(posted("Waymiro", "21AADCW9393G1Z3"))
    assert history.anomalies("Brand New Vendor", "29AAFCB7707D1ZQ", Decimal("0.18")) == []


def test_an_empty_history_is_harmless(isolated):
    assert history.build() == {}
    assert history.match("Anyone", "21AADCW9393G1Z3") is None
    assert history.anomalies("Anyone", "21AADCW9393G1Z3", Decimal("0.18")) == []
    assert history.summary() == []


# --------------------------------------------------------------------------- #
# The constraint that matters most
# --------------------------------------------------------------------------- #

def test_history_never_reaches_the_reader(isolated, monkeypatch):
    """The whole design rests on this. If a profile were fed to the extractor
    as context, the model could reconcile the document against history and
    return a GSTIN it never saw - which on a filing is the worst kind of wrong,
    because it looks right."""
    from app import pipeline

    for _ in range(3):
        store.add(posted("Waymiro", "21AADCW9393G1Z3"))

    seen: list = []
    real = pipeline.heuristic.extract

    def watched(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(pipeline.heuristic, "extract", watched)
    monkeypatch.setattr(pipeline, "has_credentials", lambda: False)

    # The reader is called with a path and nothing else. There is no parameter
    # through which history could be passed, and this pins that.
    import inspect
    signature = inspect.signature(pipeline._read_document)
    assert list(signature.parameters) == ["path"], (
        "if _read_document grows a context parameter, history could leak into the read"
    )


def test_the_profile_is_attached_for_the_reviewer(isolated):
    """It is shown, not used. as_dict is what the review screen renders."""
    for _ in range(3):
        store.add(posted("Waymiro", "21AADCW9393G1Z3", rate="0.18"))

    shown = history.match("Waymiro", "21AADCW9393G1Z3").as_dict()
    assert shown["invoice_count"] == 3
    assert shown["gstin"] == "21AADCW9393G1Z3"
    assert shown["usual_rate"] == "0.18"


def test_summary_lists_suppliers_most_active_first(isolated):
    for _ in range(4):
        store.add(posted("Busy Supplier", "21AADCW9393G1Z3"))
    store.add(posted("Quiet Supplier", "36AAGCR8772D1Z3"))

    listed = history.summary()
    assert [s["invoice_count"] for s in listed] == [4, 1]
    assert listed[0]["name"] == "Busy Supplier"
