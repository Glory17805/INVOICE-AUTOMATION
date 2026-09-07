"""Text for a document, however that text has to be obtained.

Three sources, in order of fidelity: the PDF's own text layer, the same layer
asked for with its layout preserved, and OCR for anything with no text in it
at all. Callers get a string and do not need to know which one produced it.
"""

from __future__ import annotations

from pathlib import Path

from . import ocr

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - pypdf is a hard requirement in practice
    PdfReader = None  # type: ignore[assignment]

TEXT_SUFFIXES = {".txt", ".csv", ".md"}


# Layout mode pads its lines out with non-breaking spaces, and `str.strip`
# does not treat those as whitespace. A line of text followed by fifty of them
# therefore measures fifty characters longer than it looks, which silently
# defeated every length check and every strip downstream. Flattening them here
# fixes all of it at once, and costs nothing: NBSP and space are both one
# character, so the column positions the table parser depends on do not move.
NBSP = chr(0xA0)


def _extract(path: Path, mode: str) -> str:
    if PdfReader is None or path.suffix.lower() != ".pdf":
        return ""
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text(extraction_mode=mode) or "" for page in reader.pages]
        return "\n".join(pages).replace(NBSP, " ")
    except Exception:
        return ""


def pdf_text(path: Path) -> str:
    """The embedded text of a PDF, or an empty string for a scan."""
    return _extract(path, "plain")


def layout_text(path: Path) -> str:
    """The embedded text with its horizontal positions preserved.

    Plain extraction returns a PDF's text in drawing order, one fragment per
    line, which is why the label reader has to work by looking at neighbouring
    lines. Layout mode pads the text out so a row of a table stays one line
    with its columns still lined up - which is what makes an item table
    readable at all. It costs more time and it is not always available, so it
    is a separate call rather than the default.
    """
    return _extract(path, "layout")


def _plain_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def document_text(path: Path) -> str:
    """Text for any supported input, falling back to OCR for a scan."""
    suffix = path.suffix.lower()

    if suffix in TEXT_SUFFIXES:
        return _plain_file(path)

    if suffix == ".pdf":
        text = pdf_text(path)
        if len(text.strip()) >= ocr.MEANINGFUL_TEXT:
            return text
        # Either nothing, or so little that it is page furniture rather than
        # content. Read the pixels, and keep whichever gives more.
        scanned = ocr.pdf_text(path)
        return scanned if len(scanned.strip()) > len(text.strip()) else text

    if suffix in ocr.IMAGE_SUFFIXES:
        return ocr.image_text(path)

    return ""


def document_layout_text(path: Path) -> str:
    """Layout-preserved text, with the same OCR fallback.

    OCR output is already laid out - Tesseract emits a line per line of the
    page, spaced as the page was - so a scan needs no separate treatment here.
    """
    if path.suffix.lower() == ".pdf":
        text = layout_text(path)
        if len(text.strip()) >= ocr.MEANINGFUL_TEXT:
            return text
    return document_text(path)
