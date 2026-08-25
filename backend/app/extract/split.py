"""Split a multi-invoice PDF into one document per invoice.

Billing software exports a month's invoices as a single print run - the sample
`Multiprint (40).pdf` holds 18 of them. Treating that file as one document reads
only the first invoice and silently drops the other 17, which is exactly the
"nothing slips through" promise broken.

Boundaries are found by invoice number rather than by page count, because an
invoice with a long line-item table runs onto a second page. A page whose
invoice number matches the previous page - or that has no invoice number at all -
is treated as a continuation of the invoice before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover - pypdf is a hard requirement in practice
    PdfReader = PdfWriter = None  # type: ignore[assignment]

# "Invoice No." with the value on the same line, or on the line after it.
_INLINE = re.compile(r"invoice\s*(?:no\.?|number)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})", re.I)
_LABEL = re.compile(r"^invoice\s*(?:no\.?|number)\s*[:\-]?$", re.I)


@dataclass(frozen=True)
class Segment:
    """One invoice inside a larger PDF, as an inclusive 0-based page range."""

    first_page: int
    last_page: int
    invoice_number: str | None

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1

    @property
    def label(self) -> str:
        """Human-facing page reference, 1-based."""
        if self.page_count == 1:
            return f"p{self.first_page + 1}"
        return f"p{self.first_page + 1}-{self.last_page + 1}"


def _plausible(candidate: str | None) -> str | None:
    """Reject anything that is not actually an invoice number.

    The label pattern would otherwise match prose - "the invoice number here"
    yields "here" - and a bogus number splits a file in the wrong place. Every
    real invoice number carries at least one digit.
    """
    if not candidate:
        return None
    cleaned = candidate.strip().strip(".,;:")
    if not cleaned or not any(char.isdigit() for char in cleaned):
        return None
    return cleaned


def invoice_number_on_page(text: str) -> str | None:
    """The invoice number printed on one page, if there is one."""
    if not text:
        return None

    inline = _plausible(match.group(1) if (match := _INLINE.search(text)) else None)
    if inline:
        return inline

    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not _LABEL.match(line):
            continue
        for candidate in lines[index + 1: index + 4]:
            cleaned = candidate.lstrip(":").strip()
            if not cleaned:
                continue
            return _plausible(cleaned)
    return None


def find_segments(path: Path) -> list[Segment]:
    """Split a PDF into one segment per invoice.

    A single-page PDF, a non-PDF, or a PDF with no readable invoice numbers
    comes back as one segment covering the whole file - splitting on a guess
    would be worse than not splitting, since it could tear one invoice in half.
    """
    if PdfReader is None or path.suffix.lower() != ".pdf":
        return [Segment(0, 0, None)]

    try:
        reader = PdfReader(str(path))
        pages = len(reader.pages)
    except Exception:
        return [Segment(0, 0, None)]

    if pages <= 1:
        return [Segment(0, 0, invoice_number_on_page(_page_text(reader, 0)))]

    numbers = [invoice_number_on_page(_page_text(reader, i)) for i in range(pages)]
    if not any(numbers):
        # A scan, or a layout this reader does not recognise. Keep it whole:
        # splitting on a guess could tear one invoice into two.
        return [Segment(0, pages - 1, None)]
    return group_pages(numbers)


def group_pages(numbers: list[str | None]) -> list[Segment]:
    """Group pages into invoices from the invoice number found on each page.

    A page repeating the previous page's number, or carrying none at all, is a
    continuation of the invoice already open - that is what keeps a two-page
    invoice with a long item table in one piece.
    """
    if not numbers:
        return []

    segments: list[Segment] = []
    start = 0
    current = numbers[0]
    for index in range(1, len(numbers)):
        number = numbers[index]
        if number is None or number == current:
            if current is None:
                current = number  # first number seen on a continuation run
            continue
        segments.append(Segment(start, index - 1, current))
        start, current = index, number
    segments.append(Segment(start, len(numbers) - 1, current))
    return segments


def _page_text(reader, index: int) -> str:
    try:
        return reader.pages[index].extract_text() or ""
    except Exception:
        return ""


def write_segment(source: Path, segment: Segment, destination: Path) -> Path:
    """Write one invoice's pages out as its own PDF.

    Each invoice gets a real file so the Quick Review pane shows that invoice
    alone, and so the archived audit copy is the invoice rather than the whole
    print run it arrived in.
    """
    if PdfWriter is None:
        raise RuntimeError("pypdf is required to split multi-invoice PDFs.")

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for index in range(segment.first_page, segment.last_page + 1):
        writer.add_page(reader.pages[index])
    with destination.open("wb") as handle:
        writer.write(handle)
    return destination
