"""Rules and validation tests, built from the real documents in this folder.

Every expected figure below is taken from either the sample tax invoice or an
existing row of the May-26 workbook, so a passing suite means the system
reproduces the client's own numbers rather than numbers we invented.
"""

import base64
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import IRA_INNOVATIONS
from app.gst import rules
from app.gst.states import resolve_place_of_supply, same_state, state_code_from_gstin
from app.gst.validate import ValidationResult, check_duplicate, is_valid_gstin, reconcile_tax
from app.models import DocumentType, ExtractedInvoice, SupplyType


def invoice(**kwargs) -> ExtractedInvoice:
    base = dict(
        document_type=None, invoice_number=None, invoice_date=None,
        supplier_name=None, supplier_gstin=None, supplier_address=None,
        recipient_name=None, recipient_gstin=None, recipient_address=None,
        place_of_supply=None, reverse_charge=False, is_credit_note=False,
        hsn_sac=None, line_items=[], taxable_value=None, gst_rate_percent=None,
        cgst_amount=None, sgst_amount=None, igst_amount=None, cess_amount=None,
        total_amount=None, quantity=None, notes=None,
    )
    base.update(kwargs)
    return ExtractedInvoice(**base)


# --------------------------------------------------------------------------- #
# GSTIN
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("gstin", [
    "37AAKFI3341N1Z0",   # Ira Innovations, from the GSTR-1 sheet header
    "21AADCW9393G1Z3",   # Waymiro, from GSTR-2B row 7
    "37AAMCS8857L1ZB",   # SBI General Insurance, from GSTR-2B row 8
    "36ABMCS3412L1Z2",   # SmartShift, from RCM row 8
    "36AAGCR8772D1Z3",   # Porter, from RCM row 10
    "29AAFCB7707D1ZQ",   # Swiggy, from the Credit Note sheet
    "37DTMPS2691B1ZB",   # Sri Jayagurudatta Tredars, from the legacy tabs
])
def test_real_gstins_pass_checksum(gstin):
    assert is_valid_gstin(gstin)


@pytest.mark.parametrize("gstin", [
    "37AAKFI3341N1Z1",   # last character altered
    "37AAKFI3341N1ZO",   # letter O in place of the digit zero
    "37AAKFI3341N1Z",    # too short
    "AA37KFI3341N1Z0",   # state code is not numeric
    "99ZZZZZ9999Z9ZZ",   # well-formed shape, wrong checksum
    None, "",
])
def test_bad_gstins_are_rejected(gstin):
    assert not is_valid_gstin(gstin)


# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #

def test_state_code_comes_from_the_gstin_prefix():
    assert state_code_from_gstin("21AADCW9393G1Z3") == "21"   # Odisha
    assert state_code_from_gstin("36AAGCR8772D1Z3") == "36"   # Telangana


def test_legacy_andhra_pradesh_code_does_not_break_the_split():
    """The sample invoice prints "Andhra Pradesh ** ( 28 )" against a 37 GSTIN."""
    assert resolve_place_of_supply("Andhra Pradesh ** ( 28 )") == "37"
    assert same_state("37", "28")
    assert not same_state("37", "36")


def test_place_of_supply_falls_back_to_a_bare_code():
    assert resolve_place_of_supply("29") == "29"
    assert resolve_place_of_supply("") is None


# --------------------------------------------------------------------------- #
# Classification and tax split
# --------------------------------------------------------------------------- #

def test_sample_sales_invoice_matches_the_printed_totals():
    """Multiprint (31).pdf: IRA1082026-27, Prasuna Reddy, Nellore AP."""
    doc = invoice(
        invoice_number="IRA1082026-27",
        invoice_date="31-May-2026",
        supplier_name="Ira Innovations",
        supplier_gstin=IRA_INNOVATIONS.gstin,
        recipient_name="Prasuna Reddy",
        recipient_gstin=None,
        place_of_supply="Andhra Pradesh ** ( 28 )",
        taxable_value=4881.10,
        gst_rate_percent=18,
        cgst_amount=439.30,
        sgst_amount=439.30,
        total_amount=5759.70,
    )
    t = rules.apply_gst(doc, IRA_INNOVATIONS)

    assert t.document_type is DocumentType.SALES
    assert t.supply_type is SupplyType.INTRA_STATE
    assert t.cgst == Decimal("439.30")
    assert t.sgst == Decimal("439.30")
    assert t.igst == Decimal("0.00")
    assert t.invoice_total == Decimal("5759.70")


def test_waymiro_purchase_is_inter_state_igst():
    """GSTR-2B row 7: 6,000 at 18% from an Odisha supplier - 1,080 IGST."""
    doc = invoice(
        invoice_number="10029",
        supplier_name="WAYMIRO PRIVATE LIMITED",
        supplier_gstin="21AADCW9393G1Z3",
        recipient_name="Ira Innovations",
        recipient_gstin=IRA_INNOVATIONS.gstin,
        taxable_value=6000,
        gst_rate_percent=18,
        igst_amount=1080,
        total_amount=7080,
    )
    t = rules.apply_gst(doc, IRA_INNOVATIONS)

    assert t.document_type is DocumentType.PURCHASE
    assert t.supply_type is SupplyType.INTER_STATE
    assert t.igst == Decimal("1080.00")
    assert t.cgst == t.sgst == Decimal("0.00")
    assert t.invoice_total == Decimal("7080.00")


def test_sbi_purchase_from_the_same_state_splits_into_cgst_and_sgst():
    """GSTR-2B row 8: an AP supplier, so 91.86 into each of CGST and SGST."""
    doc = invoice(
        invoice_number="133183880",
        supplier_name="SBI GENERAL INSURANCE COMPANY LTD",
        supplier_gstin="37AAMCS8857L1ZB",
        recipient_gstin=IRA_INNOVATIONS.gstin,
        taxable_value=1020.65,
        gst_rate_percent=18,
        cgst_amount=91.86,
        sgst_amount=91.86,
    )
    t = rules.apply_gst(doc, IRA_INNOVATIONS)

    assert t.supply_type is SupplyType.INTRA_STATE
    assert t.cgst == Decimal("91.86")
    assert t.sgst == Decimal("91.86")


def test_reverse_charge_bill_routes_to_the_rcm_register():
    """RCM row 10: Porter, 682 at 5% - 34.10 IGST, total stays the bill value."""
    doc = invoice(
        invoice_number="CRN1589069498",
        supplier_name="Porter",
        supplier_gstin="36AAGCR8772D1Z3",
        recipient_gstin=IRA_INNOVATIONS.gstin,
        reverse_charge=True,
        taxable_value=682,
        gst_rate_percent=5,
    )
    t = rules.apply_gst(doc, IRA_INNOVATIONS)

    assert t.document_type is DocumentType.RCM
    assert t.supply_type is SupplyType.INTER_STATE
    assert t.igst == Decimal("34.10")
    # The supplier collects no tax on a reverse-charge bill.
    assert t.invoice_total == Decimal("682.00")


def test_credit_note_routes_to_the_credit_note_register():
    """Credit Note row 6: Swiggy, 30,000 at 18% from Karnataka - 5,400 IGST."""
    doc = invoice(
        invoice_number="260513AL3P290097",
        supplier_name="Swiggy",
        supplier_gstin="29AAFCB7707D1ZQ",
        recipient_gstin=IRA_INNOVATIONS.gstin,
        is_credit_note=True,
        place_of_supply="Andhra Pradesh",
        taxable_value=30000,
        gst_rate_percent=18,
        igst_amount=5400,
    )
    t = rules.apply_gst(doc, IRA_INNOVATIONS)

    assert t.document_type is DocumentType.CREDIT_NOTE
    assert t.igst == Decimal("5400.00")
    assert t.invoice_total == Decimal("35400.00")


def test_a_credit_note_that_is_also_reverse_charge_stays_a_credit_note():
    doc = invoice(
        supplier_gstin="29AAFCB7707D1ZQ", recipient_gstin=IRA_INNOVATIONS.gstin,
        is_credit_note=True, reverse_charge=True, taxable_value=100, gst_rate_percent=18,
    )
    assert rules.apply_gst(doc, IRA_INNOVATIONS).document_type is DocumentType.CREDIT_NOTE


def test_rate_is_derived_when_the_document_prints_only_amounts():
    """The sample invoice prints 9% + 9% per line, never a combined 18%."""
    doc = invoice(
        supplier_gstin=IRA_INNOVATIONS.gstin, place_of_supply="Andhra Pradesh",
        taxable_value=4881.10, cgst_amount=439.30, sgst_amount=439.30,
    )
    assert rules.apply_gst(doc, IRA_INNOVATIONS).rate == Decimal("0.1800")


def test_a_rate_given_as_a_fraction_is_accepted():
    doc = invoice(supplier_gstin=IRA_INNOVATIONS.gstin, taxable_value=1000, gst_rate_percent=0.18)
    assert rules.apply_gst(doc, IRA_INNOVATIONS).rate == Decimal("0.1800")


def test_unknown_place_of_supply_defaults_to_the_safer_inter_state_treatment():
    doc = invoice(supplier_name="Unknown Vendor", taxable_value=500, gst_rate_percent=18)
    t = rules.apply_gst(doc, IRA_INNOVATIONS)
    assert t.supply_type is SupplyType.INTER_STATE


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_tax_mismatch_is_flagged():
    result = ValidationResult()
    reconcile_tax(
        result, stated_total_tax=Decimal("900.00"), computed_total_tax=Decimal("878.60"),
        taxable_value=Decimal("4881.10"), rate=Decimal("0.18"),
    )
    assert not result.ok
    assert result.blocking[0].code == "tax_mismatch"


def test_rounding_differences_are_tolerated():
    result = ValidationResult()
    reconcile_tax(
        result, stated_total_tax=Decimal("878.59"), computed_total_tax=Decimal("878.60"),
        taxable_value=Decimal("4881.10"), rate=Decimal("0.18"),
    )
    assert result.ok


def test_duplicate_invoice_numbers_are_caught():
    result = ValidationResult()
    check_duplicate(
        result, invoice_no="IRA1082026-27", party_gstin=None,
        existing=[("IRA1082026-27", None)],
    )
    assert not result.ok
    assert result.blocking[0].code == "duplicate_invoice"


def test_the_same_number_from_a_different_party_is_not_a_duplicate():
    result = ValidationResult()
    check_duplicate(
        result, invoice_no="10029", party_gstin="21AADCW9393G1Z3",
        existing=[("10029", "36AAGCR8772D1Z3")],
    )
    assert result.ok


@pytest.fixture
def isolated_workbook(tmp_path, monkeypatch):
    """evaluate() reads the register for duplicates, so give it its own copy."""
    from app import workbook
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path)
    return tmp_path


def _sample_sale():
    return invoice(
        invoice_number="IRA1082026-27", invoice_date="31-May-2026",
        supplier_gstin=IRA_INNOVATIONS.gstin, supplier_name="Ira Innovations",
        recipient_name="Prasuna Reddy", place_of_supply="Andhra Pradesh",
        taxable_value=4881.10, gst_rate_percent=18, cgst_amount=439.30, sgst_amount=439.30,
    )


def test_a_b2c_sale_raises_no_issues_at_all(isolated_workbook):
    """Selling to an unregistered buyer is ordinary, not something to resolve."""
    from app.pipeline import evaluate
    treatment, result = evaluate(_sample_sale())

    assert result.issues == []          # nothing for a reviewer to act on
    assert treatment.supply_category == "B2C"


def test_a_registered_customer_is_reported_as_b2b(isolated_workbook):
    from app.pipeline import evaluate
    doc = _sample_sale()
    doc.recipient_gstin = "29AAFCB7707D1ZQ"
    doc.place_of_supply = "Karnataka"
    doc.cgst_amount, doc.sgst_amount, doc.igst_amount = None, None, 878.60

    treatment, result = evaluate(doc)
    assert treatment.supply_category == "B2B"
    assert result.ok


def test_a_missing_invoice_date_blocks_posting(isolated_workbook):
    """The register row has a date column; it cannot be written without one."""
    from app.pipeline import evaluate
    doc = _sample_sale()
    doc.invoice_date = None

    _, result = evaluate(doc)
    assert not result.ok
    assert any(i.code == "invoice_date_missing" for i in result.blocking)


def test_a_missing_party_name_blocks_posting(isolated_workbook):
    from app.pipeline import evaluate
    doc = _sample_sale()
    doc.recipient_name = None

    _, result = evaluate(doc)
    assert any(i.code == "counterparty_missing" for i in result.blocking)


def test_a_clean_document_is_ready_to_post_whichever_reader_read_it(isolated_workbook):
    """Status follows the quality of the read, not which reader produced it."""
    from app.models import DocStatus
    from app.pipeline import _status_for, evaluate

    _, result = evaluate(_sample_sale())
    assert _status_for(result) is DocStatus.READY


def test_every_row_approval_mode_sends_clean_documents_to_review(isolated_workbook, monkeypatch):
    """The blueprint left this open, so it is a setting rather than a policy."""
    from app.models import DocStatus
    from app.pipeline import _status_for, evaluate

    monkeypatch.setenv("GST_APPROVAL_MODE", "every_row")
    _, result = evaluate(_sample_sale())
    assert result.ok
    assert _status_for(result) is DocStatus.NEEDS_REVIEW


# --------------------------------------------------------------------------- #
# The offline reader's real limit
# --------------------------------------------------------------------------- #

# A valid 1x1 transparent PNG - stands in for a scanned invoice: a real image
# file with no text layer for the offline reader to parse.
_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg=="
)


def test_a_scan_with_no_text_layer_is_flagged_on_that_document(tmp_path, monkeypatch):
    """The offline reader's one real failure mode has to surface, and it has to
    surface on the document it affects rather than as a standing warning.

    The offline reader is pinned deliberately. This test is *about* running
    without a model, and leaving that to whichever key happens to be in .env
    made it pass or fail on the state of somebody's billing account rather than
    on the behaviour it describes.
    """
    from app import config, pipeline, store, workbook

    monkeypatch.setattr(pipeline, "has_credentials", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path)
    (tmp_path / "incoming").mkdir(parents=True, exist_ok=True)

    records = pipeline.capture(_ONE_PIXEL_PNG, "scanned-bill.png", source="scan")
    assert len(records) == 1
    record = records[0]

    codes = {issue["code"] for issue in record["issues"]}
    assert "no_text_layer" in codes
    assert record["status"] == "needs_review"
    message = next(i["message"] for i in record["issues"] if i["code"] == "no_text_layer")
    assert "scanned-bill.png" in message
    # The message must name the key the ACTIVE provider needs. Hard-coding
    # ANTHROPIC_API_KEY here is what let it go stale when Gemini arrived.
    from app.config import extraction_provider
    expected = "GEMINI_API_KEY" if extraction_provider() == "gemini" else "ANTHROPIC_API_KEY"
    assert expected in message


def test_a_readable_pdf_is_not_flagged_for_a_missing_text_layer(tmp_path, monkeypatch):
    """The complement: a normal PDF must not pick up the scan warning."""
    from app import config, pipeline, store, workbook

    monkeypatch.setattr(pipeline, "has_credentials", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path)
    (tmp_path / "incoming").mkdir(parents=True, exist_ok=True)

    sample = Path(__file__).resolve().parents[3] / "Multiprint (31).pdf"
    if not sample.exists():
        pytest.skip("sample invoice not present")

    records = pipeline.capture(sample.read_bytes(), sample.name, source="upload")
    assert len(records) == 1, "a single-invoice PDF must not be split"
    record = records[0]
    codes = {issue["code"] for issue in record["issues"]}
    assert "no_text_layer" not in codes
    assert record["status"] == "ready"


def test_an_unreadable_scan_reports_one_cause_not_seven_symptoms(tmp_path, monkeypatch):
    """No text layer means every other check fails too. Report the cause alone."""
    from app import config, pipeline, store, workbook

    monkeypatch.setattr(pipeline, "has_credentials", lambda: False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path)
    (tmp_path / "incoming").mkdir(parents=True, exist_ok=True)

    record = pipeline.capture(_ONE_PIXEL_PNG, "scan.png", source="scan")[0]
    assert [i["code"] for i in record["issues"]] == ["no_text_layer"]


# --------------------------------------------------------------------------- #
# The two things an offline read gets silently wrong
# --------------------------------------------------------------------------- #

class TestReverseChargeWording:
    """The answer may sit some way behind the words "reverse charge"."""

    @pytest.mark.parametrize("text", [
        "Reverse Charge: Yes",
        "Reverse charge - applicable",
        "Whether tax payable on reverse charge basis\nYes",
        "Whether tax payable on reverse charge basis : Yes",
        "REVERSE CHARGE APPLICABLE",
    ])
    def test_it_is_read_as_yes(self, text):
        from app.extract.heuristic import _reverse_charge
        assert _reverse_charge(text) is True

    @pytest.mark.parametrize("text", [
        "Reverse Charge: No",
        "Reverse charge - Not Applicable",
        "Whether tax payable on reverse charge basis\nNo",
        "Whether tax payable on reverse charge basis\nN/A",
        "Reverse charge",                       # named but unanswered
        "Goods sold under normal charge",       # never mentioned
    ])
    def test_it_is_read_as_no(self, text):
        from app.extract.heuristic import _reverse_charge
        assert _reverse_charge(text) is False

    def test_a_later_yes_cannot_override_an_explicit_no(self):
        """The first answer after the phrase wins.

        Claiming RCM where the document denies it moves the tax liability to
        the wrong party, so a distant "Yes" must not be borrowed.
        """
        from app.extract.heuristic import _reverse_charge
        assert _reverse_charge("Reverse Charge: No\nE-way bill required: Yes") is False


class TestRateMustBeStatutory:
    """A rate that is not a GST slab means the figures behind it were misread."""

    @pytest.mark.parametrize("rate", ["0.0025", "0.005", "0.03", "0.05", "0.12", "0.18", "0.28"])
    def test_every_real_slab_passes(self, rate):
        from app.gst.validate import ValidationResult, check_rate_is_statutory
        result = ValidationResult()
        check_rate_is_statutory(result, Decimal(rate))
        assert result.issues == []

    def test_an_invoices_own_rounding_does_not_trip_it(self):
        """18% on an odd taxable value derives 17.9997%, which is still 18%."""
        from app.gst.validate import ValidationResult, check_rate_is_statutory
        result = ValidationResult()
        check_rate_is_statutory(result, Decimal("0.179997"))
        assert result.issues == []

    def test_two_rates_averaged_together_are_caught(self):
        """5% and 18% goods on one bill derive 13.67% - not a rate that exists."""
        from app.gst.validate import ValidationResult, check_rate_is_statutory
        result = ValidationResult()
        check_rate_is_statutory(result, Decimal("0.1367"))
        assert [i.code for i in result.blocking] == ["rate_not_statutory"]

    def test_a_missing_rate_is_left_to_the_check_that_owns_it(self):
        from app.gst.validate import ValidationResult, check_rate_is_statutory
        result = ValidationResult()
        check_rate_is_statutory(result, Decimal("0"))
        assert result.issues == []
