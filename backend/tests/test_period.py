"""Return periods are derived from invoices, never configured.

A configured period means the application only works for whichever month it was
last pointed at, and silently files July sales into a May return whenever
someone forgets to change it. These tests pin the alternative: the period comes
from the invoice's own date, and each period gets its own workbook.
"""

from datetime import date
from decimal import Decimal

import pytest

from app import period as periods
from app import workbook
from app.models import DocumentType

from .test_gst import invoice

# --------------------------------------------------------------------------- #
# Deriving a period
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    (date(2026, 5, 31), "May-26"),
    (date(2026, 7, 8), "Jul-26"),
    (date(2026, 1, 1), "Jan-26"),
    (date(2025, 12, 31), "Dec-25"),
    (date(2030, 3, 15), "Mar-30"),
])
def test_a_period_is_the_month_the_invoice_was_raised_in(value, expected):
    assert periods.period_of(value) == expected


@pytest.mark.parametrize("text,expected", [
    ("May-26", (2026, 5)), ("Jul-26", (2026, 7)), ("Dec-25", (2025, 12)),
    ("not a period", None), ("", None), (None, None), ("13-26", None),
])
def test_periods_round_trip(text, expected):
    assert periods.parse_period(text) == expected


@pytest.mark.parametrize("header,expected", [
    ("Return Period : May-26", "May-26"),
    ("Return Period: Jul-2026", "Jul-26"),
    ("return period - december-25", "Dec-25"),
    ("Sales", None),
    (None, None),
])
def test_a_workbook_declares_its_own_period_in_its_header(header, expected):
    assert periods.period_from_header(header) == expected


@pytest.mark.parametrize("period,expected", [
    ("May-26", "Apr-26"),
    ("Jan-26", "Dec-25"),   # the year rolls back
    ("Apr-26", "Mar-26"),
])
def test_the_previous_period_is_the_month_before(period, expected):
    assert periods.previous(period) == expected


def test_periods_sort_chronologically_not_alphabetically():
    """'Apr' sorts before 'May' alphabetically but 'Dec' does not sort last."""
    unsorted = ["Dec-25", "May-26", "Jan-26", "Apr-26"]
    assert sorted(unsorted, key=periods.sort_key) == ["Dec-25", "Jan-26", "Apr-26", "May-26"]


# --------------------------------------------------------------------------- #
# One workbook per period
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def scratch_workbooks(tmp_path, monkeypatch):
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path)
    return tmp_path


def test_the_master_workbook_declares_which_period_it_covers():
    """Read from GSTR-1!A5, not from configuration."""
    assert workbook.master_period() == "May-26"


def test_the_masters_own_period_keeps_its_existing_rows():
    workbook.ensure_working_copy("May-26")
    # The workbook's real May purchases, credit note and RCM rows survive.
    assert len(workbook.read_register(DocumentType.PURCHASE, "May-26")) == 2
    assert len(workbook.read_register(DocumentType.CREDIT_NOTE, "May-26")) == 1
    assert len(workbook.read_register(DocumentType.RCM, "May-26")) == 3


def test_a_later_period_starts_empty():
    """July's workbook must not inherit May's purchases."""
    workbook.ensure_working_copy("Jul-26")
    for doc_type in DocumentType:
        assert workbook.read_register(doc_type, "Jul-26") == [], doc_type


def test_a_new_period_carries_its_own_header():
    from openpyxl import load_workbook

    workbook.ensure_working_copy("Jul-26")
    ws = load_workbook(workbook.workbook_path("Jul-26"))["GSTR-1"]
    assert ws["A5"].value == "Return Period : Jul-26"


def test_a_new_period_starts_with_no_opening_credit():
    """Carrying May's credit into July would overstate it, which understates
    the tax due - the expensive direction to be wrong in."""
    # Jun-26 does not exist here, so Jul-26 has no predecessor to inherit from
    # and must not invent one (gap G2 carries it forward only when it is known).
    workbook.ensure_working_copy("Jul-26")
    summary = workbook.tax_payable_summary("Jul-26")
    assert summary.itc_carry_forward == {"igst": "0.00", "cgst": "0.00", "sgst": "0.00"}
    assert workbook.opening_credit_is_unset("Jul-26")

    # The master's own period keeps its real opening figures.
    workbook.ensure_working_copy("May-26")
    assert Decimal(workbook.tax_payable_summary("May-26").itc_carry_forward["igst"]) == Decimal("620777")
    assert not workbook.opening_credit_is_unset("May-26")


def test_periods_do_not_leak_into_each_other():
    """The same invoice number in two periods is two different rows."""
    def post(number, invoice_date, period):
        from app.config import IRA_INNOVATIONS
        from app.gst import rules
        doc = invoice(
            invoice_number=number, invoice_date=invoice_date,
            supplier_gstin=IRA_INNOVATIONS.gstin, supplier_name="Ira Innovations",
            recipient_name="Prasuna Reddy", place_of_supply="Andhra Pradesh",
            taxable_value=1000, gst_rate_percent=18,
        )
        treatment = rules.apply_gst(doc, IRA_INNOVATIONS)
        return workbook.post_row(treatment, {
            "period": period, "invoice_number": number,
            "invoice_date_obj": workbook.parse_date(invoice_date),
            "hsn_sac": None, "quantity": None, "unit_rate": None,
        }, period)

    post("INV-1", "31-May-2026", "May-26")
    post("INV-1", "31-Jul-2026", "Jul-26")

    assert len(workbook.read_register(DocumentType.SALES, "May-26")) == 1
    assert len(workbook.read_register(DocumentType.SALES, "Jul-26")) == 1
    # A duplicate is only a duplicate within its own return.
    assert workbook.posted_keys(DocumentType.SALES, "May-26") == [("INV-1", None)]
    assert workbook.posted_keys(DocumentType.SALES, "Jul-26") == [("INV-1", None)]


def test_available_periods_lists_every_workbook_newest_first():
    for period in ("May-26", "Jul-26", "Jan-26"):
        workbook.ensure_working_copy(period)
    assert workbook.available_periods() == ["Jul-26", "May-26", "Jan-26"]


def test_an_invalid_period_is_refused():
    with pytest.raises(workbook.WorkbookError):
        workbook.ensure_working_copy("not-a-period")


# --------------------------------------------------------------------------- #
# The pipeline routes by the invoice's own date
# --------------------------------------------------------------------------- #

def test_the_pipeline_derives_the_period_from_the_invoice():
    from app.pipeline import period_for

    assert period_for(invoice(invoice_date="28-Jul-2026")) == "Jul-26"
    assert period_for(invoice(invoice_date="31-May-2026")) == "May-26"
    assert period_for(invoice(invoice_date="08/12/2025")) == "Dec-25"


def test_an_invoice_with_no_readable_date_has_no_period():
    """And is therefore blocked, because there is no return to file it against."""
    from app.pipeline import evaluate, period_for

    doc = invoice(invoice_number="X-1", supplier_gstin="37AAKFI3341N1Z0",
                  supplier_name="Ira Innovations", recipient_name="Someone",
                  place_of_supply="Andhra Pradesh", taxable_value=100, gst_rate_percent=18)
    assert period_for(doc) is None

    _, result = evaluate(doc)
    assert any(i.code == "invoice_date_missing" for i in result.blocking)
