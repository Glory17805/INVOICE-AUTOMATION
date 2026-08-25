"""Multi-invoice PDF splitting.

Billing software exports a whole month as one print run - `Multiprint (40).pdf`
holds 18 invoices. Reading only the first one and dropping the rest is the
worst failure this system can have, because it is silent.
"""

from pathlib import Path

import pytest

from app.extract.heuristic import extract as heuristic_extract
from app.extract.split import Segment, find_segments, group_pages, invoice_number_on_page

SAMPLES = Path(__file__).resolve().parents[3]   # d:/IRA, where the samples live
MULTI = SAMPLES / "Multiprint (40).pdf"
SINGLE = SAMPLES / "Multiprint (31).pdf"


@pytest.fixture(scope="session")
def print_run():
    """Segment the 18-page print run once and share it across the module."""
    if not MULTI.exists():
        pytest.skip("Multiprint (40).pdf not present")
    return find_segments(MULTI)


@pytest.fixture(scope="session")
def print_run_parts(print_run, tmp_path_factory):
    """Each invoice written out once, then reused by every reader test."""
    from app.extract.split import write_segment

    folder = tmp_path_factory.mktemp("print-run")
    return [
        (segment, write_segment(MULTI, segment, folder / f"{segment.label}.pdf"))
        for segment in print_run
    ]


# --------------------------------------------------------------------------- #
# Grouping pages into invoices
# --------------------------------------------------------------------------- #

def test_one_invoice_per_page():
    assert group_pages(["A", "B", "C"]) == [
        Segment(0, 0, "A"), Segment(1, 1, "B"), Segment(2, 2, "C"),
    ]


def test_a_repeated_number_means_the_invoice_runs_onto_a_second_page():
    """A long item table spills over; the invoice must not be torn in half."""
    assert group_pages(["A", "A", "B"]) == [Segment(0, 1, "A"), Segment(2, 2, "B")]


def test_a_page_with_no_number_continues_the_invoice_before_it():
    """Continuation pages often omit the header entirely."""
    assert group_pages(["A", None, None, "B"]) == [Segment(0, 2, "A"), Segment(3, 3, "B")]


def test_a_leading_page_with_no_number_does_not_swallow_the_first_invoice():
    assert group_pages([None, "A", "B"]) == [
        Segment(0, 0, None), Segment(1, 1, "A"), Segment(2, 2, "B"),
    ]


def test_a_single_page_is_one_segment():
    assert group_pages(["A"]) == [Segment(0, 0, "A")]


def test_page_labels_read_as_page_references():
    assert Segment(0, 0, "A").label == "p1"
    assert Segment(2, 4, "A").label == "p3-5"
    assert Segment(2, 4, "A").page_count == 3


# --------------------------------------------------------------------------- #
# Reading the number off a page
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("Invoice No.\nIRA1832026-27\nInvoice Date", "IRA1832026-27"),
    ("Invoice No. IRA1082026-27", "IRA1082026-27"),
    ("Invoice Number : 10029", "10029"),
    ("Invoice No.\n:\nABC-1/26", "ABC-1/26"),
    ("no invoice number here", None),
    ("", None),
])
def test_invoice_number_is_read_off_a_page(text, expected):
    assert invoice_number_on_page(text) == expected


# --------------------------------------------------------------------------- #
# The real files
# --------------------------------------------------------------------------- #

def test_the_real_print_run_splits_into_every_invoice_it_holds(print_run):
    segments = print_run
    assert len(segments) == 18

    numbers = [s.invoice_number for s in segments]
    # Sequential and descending, as the print run has them.
    assert numbers[0] == "IRA1832026-27"
    assert numbers[-1] == "IRA1662026-27"
    assert len(set(numbers)) == 18, "every invoice must be distinct"
    # Every page accounted for, none covered twice.
    assert sum(s.page_count for s in segments) == 18
    assert segments[0].first_page == 0
    assert segments[-1].last_page == 17


@pytest.mark.skipif(not SINGLE.exists(), reason="Multiprint (31).pdf not present")
def test_a_single_invoice_file_is_not_split():
    segments = find_segments(SINGLE)
    assert len(segments) == 1
    assert segments[0].invoice_number == "IRA1082026-27"


def test_a_file_with_no_text_layer_is_left_whole(tmp_path):
    """Splitting on a guess would be worse than not splitting."""
    fake = tmp_path / "not-a-pdf.pdf"
    fake.write_bytes(b"not really a pdf")
    assert find_segments(fake) == [Segment(0, 0, None)]


# --------------------------------------------------------------------------- #
# Totals parsing on the layouts inside the print run
# --------------------------------------------------------------------------- #

def test_every_invoice_in_the_print_run_yields_a_taxable_value(print_run_parts):
    """Two of these invoices print the unit inside the totals row
    ('Total / 1,100.00 / PKT / 17,796.61'), which used to stop the parser."""
    missing = [
        segment.invoice_number
        for segment, part in print_run_parts
        if not heuristic_extract(part).taxable_value
    ]
    assert missing == [], f"no taxable value read for {missing}"


def test_the_unit_in_a_totals_row_does_not_hide_the_amounts(print_run_parts):
    """Page 3 is Poonam Beauty Center: 17,796.61 taxable, 1,601.69 each way."""
    part = next(p for s, p in print_run_parts if s.invoice_number == "IRA1812026-27")

    doc = heuristic_extract(part)
    assert doc.taxable_value == pytest.approx(17796.61)
    assert doc.cgst_amount == pytest.approx(1601.69)
    assert doc.sgst_amount == pytest.approx(1601.69)
    assert doc.total_amount == pytest.approx(20999.99)


# --------------------------------------------------------------------------- #
# End to end: one upload becomes one document per invoice
# --------------------------------------------------------------------------- #

def test_capturing_a_print_run_creates_a_document_per_invoice(tmp_path, monkeypatch):
    """The whole point: 18 invoices in, 18 documents out, none dropped."""
    if not MULTI.exists():
        pytest.skip("Multiprint (40).pdf not present")

    from app import config, pipeline, store, workbook

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.json")
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path)
    (tmp_path / "incoming").mkdir(parents=True, exist_ok=True)

    records = pipeline.capture(MULTI.read_bytes(), MULTI.name, source="upload")

    assert len(records) == 18
    numbers = [r["extracted"]["invoice_number"] for r in records]
    assert len(set(numbers)) == 18, "each document must be a distinct invoice"
    assert "IRA1832026-27" in numbers and "IRA1662026-27" in numbers

    # Every document carries its provenance back to the file it arrived in.
    for position, record in enumerate(records, start=1):
        assert record["source_document"] == MULTI.name
        assert record["position"] == position
        assert record["of"] == 18
        assert Path(record["stored_path"]).exists(), "each invoice needs its own file"

    # Every one of them read a taxable value and a party.
    assert all(r["treatment"]["taxable_value"] not in (None, "0.00") for r in records)
    assert all(r["treatment"]["counterparty_name"] for r in records)

    # The staging copy is cleaned up; only the 18 parts remain.
    assert not list((tmp_path / "incoming").glob("staged__*"))
