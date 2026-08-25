"""Text layer extraction, used by the heuristic reader and to enrich LLM prompts."""

from __future__ import annotations

from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - pypdf is a hard requirement in practice
    PdfReader = None  # type: ignore[assignment]


def pdf_text(path: Path) -> str:
    """Return the embedded text of a PDF, or an empty string for a scan."""
    if PdfReader is None or path.suffix.lower() != ".pdf":
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def document_text(path: Path) -> str:
    """Text for any supported input - PDF text layer, or a plain text file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_text(path)
    if suffix in {".txt", ".csv", ".md"}:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""
