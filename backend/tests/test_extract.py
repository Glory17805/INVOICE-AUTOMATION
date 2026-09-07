"""The reader's new parts: labels, the item table, amounts, templates, OCR.

Each fixture here is an invoice shape the old reader got wrong, so a failure
in this file is a regression to a behaviour that was already paid for once.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.extract import amounts as amounts_reader
from app.extract import lineitems, ocr, templates
from app.extract.confidence import HIGH, LOW, MEDIUM, NONE
from app.extract.labels import FIELD_ALIASES, Page, to_amount

# --------------------------------------------------------------------------- #
# Labels: the same value, however the invoice writes it
# --------------------------------------------------------------------------- #

class TestSameLineLayouts:
    """A label and its value on one line, which used to yield nothing."""

    TEXT = (
        "TAX INVOICE\n"
        "Invoice No: INV-2026-118        Invoice Date: 12-Jul-2026\n"
        "Supplier: Sharma Traders        GSTIN: 37AAACS1234F1Z5\n"
        "Bill To: Ira Innovations\n"
        "Place of Supply: Andhra Pradesh (37)\n"
        "Grand Total: 11800.00\n"
    )

    @pytest.mark.parametrize("field,expected", [
        ("invoice_number", "INV-2026-118"),
        ("invoice_date", "12-Jul-2026"),
        ("supplier_name", "Sharma Traders"),
        ("gstin", "37AAACS1234F1Z5"),
        ("customer_name", "Ira Innovations"),
        ("place_of_supply", "Andhra Pradesh (37)"),
        ("total", "11800.00"),
    ])
    def test_each_field_is_found(self, field, expected):
        assert Page(self.TEXT).value(field) == expected

    def test_a_value_stops_where_the_next_label_starts(self):
        """The whole point of scanning for every label before reading any.

        Without it the invoice number would run on to the end of the line and
        swallow the date beside it.
        """
        assert Page(self.TEXT).value("invoice_number") == "INV-2026-118"


class TestNextLineLayouts:
    """The convention the current vendor uses, which must not regress."""

    TEXT = (
        "Invoice No.\nIRA1832026-27\n"
        "Invoice Date\n26-Jul-2026\n"
        "Place of\nAndhra Pradesh ** ( 28 )\n"
        "Taxable Amount\n4258.47\n"
    )

    def test_the_abbreviating_full_stop_is_not_the_value(self):
        """"Invoice No." matches the alias "invoice no" and leaves a stop
        behind; reading that as the value hides the real one below it."""
        assert Page(self.TEXT).value("invoice_number") == "IRA1832026-27"

    def test_a_split_label_still_resolves(self):
        assert Page(self.TEXT).value("place_of_supply") == "Andhra Pradesh ** ( 28 )"


class TestAValueIsNeverBorrowed:
    """A field with nothing after it must read as nothing."""

    def test_a_dash_meaning_none_is_not_a_value(self):
        page = Page("GSTIN\n-\nPlace of Supply\nKarnataka\n")
        assert page.value("gstin") is None
        assert page.value("place_of_supply") == "Karnataka"

    def test_an_empty_field_does_not_take_the_next_ones(self):
        page = Page("Invoice No.\nInvoice Date\n26-Jul-2026\n")
        assert page.value("invoice_number") is None
        assert page.value("invoice_date") == "26-Jul-2026"


@pytest.mark.parametrize("text,field,expected", [
    ("Bill No: B-9", "invoice_number", "B-9"),
    ("Tax Invoice No : TI/2026/44", "invoice_number", "TI/2026/44"),
    ("Invoice # 7781", "invoice_number", "7781"),
    ("Invoice Number - INV-5", "invoice_number", "INV-5"),
    ("Dated: 01-Apr-2026", "invoice_date", "01-Apr-2026"),
    ("Bill Date 5-May-2026", "invoice_date", "5-May-2026"),
    ("Total Invoice Value : 11,800.00", "total", "11,800.00"),
    ("Freight Charges: 250.00", "freight", "250.00"),
])
def test_alias_vocabulary(text, field, expected):
    assert Page(text).value(field) == expected


@pytest.mark.parametrize("text,expected", [
    ("1,234.56", "1234.56"),
    ("-0.50", "-0.50"),
    ("(0.50)", "-0.50"),
    ("Rs. 900", "900"),
])
def test_amounts_are_read_with_their_sign(text, expected):
    """A leading minus is a sign, not punctuation to be stripped.

    Losing it turned a deduction into an addition, which is a two-for-one
    error: the figure is wrong and the invoice stops balancing.
    """
    assert to_amount(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "-", "n/a", "10,683.00 12,605.94"])
def test_what_is_not_a_single_amount_is_not_read_as_one(text):
    assert to_amount(text) is None


def test_an_amount_is_searched_from_the_foot_of_the_page():
    """The words naming an amount in the summary also name a table column.

    Top-down finds the column heading and returns the row of sub-headings
    beneath it. The summary block is at the bottom, so that is where to look.
    """
    page = Page(
        "Taxable Value      Amount\n"
        "%                  Amount\n"
        "1  Tissue          10,683.00\n"
        "Taxable Amount     10,683.00\n"
    )
    assert page.amount("taxable_value") == Decimal("10683.00")


# --------------------------------------------------------------------------- #
# The item table
# --------------------------------------------------------------------------- #

def row(*cells: tuple[int, str]) -> str:
    """One line of a layout-mode table, with each cell at an exact column.

    Written this way rather than typed as a wide string literal because the
    parser assigns a value to a column by where it sits: a fixture whose
    columns are a few characters out is testing a table nobody would print,
    and lining one up by eye in a source file is guesswork.
    """
    line = ""
    for start, text in cells:
        line = line.ljust(start) + text
    return line + "\n"


# A layout-mode rendering: columns line up, and a banner spans its
# sub-columns without overlapping the column to its left.
TWO_RATES = (
    row((1, "Sr."), (73, "CGST"), (93, "SGST"))
    + row((1, "No."), (7, "Name of Product"), (30, "HSN / SAC"), (42, "Qty"),
          (52, "Taxable Value"))
    + row((70, "%"), (78, "Amount"), (90, "%"), (98, "Amount"))
    + row((1, "1"), (7, "Rice"), (30, "1006"), (42, "10"), (52, "1000.00"),
          (69, "2.50"), (78, "25.00"), (89, "2.50"), (98, "25.00"))
    + row((1, "2"), (7, "Pipes"), (30, "3917"), (42, "5"), (52, "2000.00"),
          (69, "9.00"), (78, "180.00"), (89, "9.00"), (98, "180.00"))
    + row((20, "Total"), (52, "3000.00"), (78, "205.00"), (98, "205.00"))
)


class TestItemTable:
    def test_every_row_is_read(self):
        items = lineitems.parse(TWO_RATES)
        assert [i.description for i in items] == ["Rice", "Pipes"]
        assert [i.hsn for i in items] == ["1006", "3917"]
        assert [i.taxable_value for i in items] == [Decimal("1000.00"), Decimal("2000.00")]

    def test_a_cgst_column_is_half_the_rate(self):
        """The other half is the SGST column beside it, so 2.5% + 2.5% is a
        5% line and 9% + 9% is an 18% one."""
        assert lineitems.distinct_rates(lineitems.parse(TWO_RATES)) == [
            Decimal("5.00"), Decimal("18.00")]

    def test_the_totals_row_is_not_an_item(self):
        assert len(lineitems.parse(TWO_RATES)) == 2

    def test_several_hsn_codes_are_all_kept(self):
        assert lineitems.distinct_hsn(lineitems.parse(TWO_RATES)) == ["1006", "3917"]

    def test_a_banner_does_not_claim_the_columns_beside_it(self):
        """"Rate" and "Taxable Value" sit on the same line as the CGST banner,
        not under it, so they are their own columns."""
        table = lineitems.find_table(TWO_RATES)
        assert table.column("taxable") is not None
        assert table.column("cgst_pct") is not None
        assert table.column("cgst_amount") is not None

    def test_text_with_no_table_yields_no_items(self):
        assert lineitems.parse("Invoice No: 1\nTotal: 100\n") == []


# --------------------------------------------------------------------------- #
# Amounts
# --------------------------------------------------------------------------- #

def _amounts(text: str) -> amounts_reader.Amounts:
    return amounts_reader.read(Page(text), [])


class TestTheInvoiceBalances:
    """Every one of these emptied every amount under the old reader."""

    def test_a_round_off_line(self):
        a = _amounts("Taxable Amount 9999.50\nAdd : CGST 899.96\nAdd : SGST 899.96\n"
                     "Round Off 0.58\nTotal Amount After Tax 11800.00\n")
        assert a.balanced and a.taxable == Decimal("9999.50")

    def test_cess(self):
        a = _amounts("Taxable Amount 10000.00\nAdd : CGST 1400.00\nAdd : SGST 1400.00\n"
                     "Add : Cess 1200.00\nTotal Amount After Tax 14000.00\n")
        assert a.balanced and a.cess == Decimal("1200.00")

    def test_freight_and_packing(self):
        a = _amounts("Taxable Amount 10000.00\nFreight Charges 250.00\nPacking Charges 150.00\n"
                     "Add : CGST 936.00\nAdd : SGST 936.00\nTotal Amount After Tax 12272.00\n")
        assert a.balanced and a.freight == Decimal("250.00")

    def test_a_discount(self):
        a = _amounts("Taxable Amount 10000.00\nLess : Discount 500.00\nAdd : CGST 855.00\n"
                     "Add : SGST 855.00\nTotal Amount After Tax 11210.00\n")
        assert a.balanced and a.discount == Decimal("500.00")

    def test_an_unprinted_round_off_is_inferred(self):
        """Six paise between the rows and the payable line, which is a
        rounding adjustment rather than a misread figure."""
        a = _amounts("Taxable Amount 10683.00\nAdd : CGST 961.47\nAdd : SGST 961.47\n"
                     "Total Amount After Tax 12606.00\n")
        assert a.balanced and a.rounding_inferred
        assert a.round_off == Decimal("0.06")

    def test_a_real_disagreement_is_still_reported(self):
        """Inferring a round-off must not become a licence to absorb anything."""
        a = _amounts("Taxable Amount 10000.00\nAdd : CGST 900.00\nAdd : SGST 900.00\n"
                     "Total Amount After Tax 99999.00\n")
        assert not a.balanced
        assert a.residual == Decimal("88199.00")


@pytest.mark.parametrize("text", [
    "Taxable Amount 10000.00\nFreight Charges 400.00\nAdd : CGST 936.00\n"
    "Add : SGST 936.00\nTotal Amount After Tax 12272.00\n",
    "Taxable Amount 10000.00\nLess : Discount 500.00\nAdd : CGST 855.00\n"
    "Add : SGST 855.00\nTotal Amount After Tax 11210.00\n",
])
def test_the_rate_is_taken_on_what_is_actually_charged(text):
    """GST is charged on the consideration, which freight adds to and a
    discount reduces. Dividing by the bare taxable value makes an ordinary
    18% bill read as 18.72% - and that would be flagged as an impossible rate.
    """
    assert amounts_reader.rate_percent(_amounts(text)) == Decimal("18.00")


# --------------------------------------------------------------------------- #
# Vendor templates
# --------------------------------------------------------------------------- #

class TestTemplates:
    def test_an_unknown_label_becomes_readable(self):
        template = templates.Template(name="Sharma", match_gstin="37AAACS1234F1Z5",
                                      labels={"invoice_number": ["challan no"]})
        assert Page("Challan No: CH-77").value("invoice_number") is None
        vocabulary = templates.label_aliases(template, FIELD_ALIASES)
        assert Page("Challan No: CH-77", vocabulary).value("invoice_number") == "CH-77"

    def test_a_template_adds_and_never_replaces(self):
        """A vendor with one odd label still prints the other fields normally,
        so a template that replaced the vocabulary would lose them."""
        template = templates.Template(name="Sharma", match_gstin="X",
                                      labels={"invoice_number": ["challan no"]})
        vocabulary = templates.label_aliases(template, FIELD_ALIASES)
        page = Page("Challan No: CH-77   Invoice Date: 12-Jul-2026", vocabulary)
        assert page.value("invoice_date") == "12-Jul-2026"

    def test_a_gstin_identifies_the_vendor(self):
        template = templates.Template(name="S", match_gstin="37AAACS1234F1Z5")
        assert template.matches("GSTIN 37AAACS1234F1Z5 here")
        assert not template.matches("someone else entirely")

    def test_every_phrase_must_appear(self):
        """One common phrase would claim invoices it knows nothing about."""
        template = templates.Template(name="S", match_text=["acme corp", "invoice"])
        assert template.matches("ACME CORP\nTax Invoice\n")
        assert not template.matches("Tax Invoice from somebody else")

    def test_an_alias_cannot_smuggle_in_a_pattern(self):
        """A template is config. Config that can inject a regex into the label
        scanner is config that can stop the reader matching anything."""
        assert templates._clean(["ok name", "(?:x" * 20, "", "a" * 90]) == ["ok name"]

    def test_a_template_matching_nothing_is_ignored(self, tmp_path, monkeypatch):
        path = tmp_path / "broken.json"
        path.write_text(json.dumps({"name": "Nobody"}), encoding="utf-8")
        monkeypatch.setattr(templates, "TEMPLATE_DIR", tmp_path)
        templates.refresh()
        assert templates.all_templates() == ()
        templates.refresh()


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #

class TestOcrDegradesRatherThanFailing:
    """No Tesseract on the machine must mean "cannot read this one", not a
    crash and not an import error."""

    def test_status_says_why_when_it_is_missing(self, monkeypatch):
        monkeypatch.setattr(ocr, "_binary", lambda: None)
        ocr.refresh()
        assert not ocr.available()
        assert "Tesseract" in ocr.status()
        ocr.refresh()

    def test_reading_returns_nothing_rather_than_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ocr, "_binary", lambda: None)
        ocr.refresh()
        scan = tmp_path / "scan.pdf"
        scan.write_bytes(b"%PDF-1.4\ntrailer<<>>\n%%EOF")
        assert ocr.read(scan) == ""
        assert "by hand" in ocr.unavailable_note(scan)
        ocr.refresh()


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #

def _report(**over):
    from app.extract.confidence import assess
    base = dict(
        invoice_number="INV-1", invoice_date="12-Jul-2026",
        invoice_date_parsed=__import__("datetime").date(2026, 7, 12),
        supplier_gstin="37AAKFI3341N1Z0", customer_gstin=None,
        supplier_name="Ira Innovations", customer_name="Mahaveer Plastics",
        place_of_supply="Andhra Pradesh", place_of_supply_code="37",
        hsn="4803", rate=Decimal("18"),
        amounts=_amounts("Taxable Amount 10000.00\nAdd : CGST 900.00\n"
                         "Add : SGST 900.00\nTotal Amount After Tax 11800.00\n"),
        items=[], source="text layer", is_sale=True,
    )
    base.update(over)
    return assess(**base)


class TestConfidence:
    def test_a_valid_gstin_is_high(self):
        assert _report().level_of("supplier_gstin") == HIGH

    def test_a_gstin_failing_its_checksum_is_low(self):
        assert _report(supplier_gstin="37AAKFI3341N1Z9").level_of("supplier_gstin") == LOW

    def test_a_date_that_will_not_parse_is_low(self):
        assert _report(invoice_date="whenever", invoice_date_parsed=None) \
            .level_of("invoice_date") == LOW

    def test_a_rate_off_every_slab_is_low(self):
        assert _report(rate=Decimal("13.67")).level_of("gst_rate_percent") == LOW

    def test_rows_carrying_two_rates_are_low(self):
        items = lineitems.parse(TWO_RATES)
        assert _report(items=items, rate=Decimal("18")).level_of("gst_rate_percent") == LOW

    def test_several_hsn_codes_are_low(self):
        items = lineitems.parse(TWO_RATES)
        assert _report(items=items).level_of("hsn_sac") == LOW

    def test_nothing_read_by_ocr_is_ever_high(self):
        """OCR guesses characters, and every figure on an invoice is
        characters. A scan is always worth a person's eyes."""
        report = _report(source="OCR")
        assert all(f.level != HIGH for f in report.fields)

    def test_the_overall_verdict_is_the_weakest_critical_field(self):
        assert _report(supplier_gstin=None).overall == NONE
        assert _report().overall in {MEDIUM, HIGH}

    def test_a_missing_customer_gstin_is_not_a_fault(self):
        """A B2C sale has no customer GSTIN, and that is normal."""
        assert _report(customer_gstin=None).level_of("recipient_gstin") == MEDIUM
