"""Stage 3 of the pipeline: read the document with Claude.

The blueprint's reasoning for an LLM reader over a trained document-AI model is
that Ira Innovations' invoice layouts already differ between vendors, and a new
layout should not require retraining. Claude reads the PDF directly against the
fixed `ExtractedInvoice` schema, so a new vendor format costs nothing.

The reader is told, explicitly, not to decide the tax treatment. Its job is to
report what is printed on the page; `gst/rules.py` owns every judgment call.
"""

from __future__ import annotations

import base64
from pathlib import Path

from ..config import IRA_INNOVATIONS, extraction_model, setting
from ..models import ExtractedInvoice
from .pdftext import document_text

SYSTEM_PROMPT = f"""\
You read Indian GST documents for {IRA_INNOVATIONS.name} (GSTIN {IRA_INNOVATIONS.gstin}, \
{IRA_INNOVATIONS.state_name}) and report exactly what is printed on them.

Rules:
- Report values as printed. Do not compute, correct, or reconcile anything.
- If a field is not present on the document, return null. Never invent a value,
  and never carry a value over from a different field because it looks plausible.
- GSTINs are 15 characters, uppercase, no spaces. Transcribe them character by
  character; a single wrong character makes the number fail its checksum.
- taxable_value is the total value before tax. total_amount is the grand total
  including tax. If the document shows per-line 9% CGST and 9% SGST, then
  gst_rate_percent is 18.
- reverse_charge is true only when the document explicitly says reverse charge
  applies. Many invoices print "Reverse Charge: No" - that is false.
- Amounts are plain numbers: no currency symbols, no thousands separators.
- Use `notes` for anything a human reviewer should know: an unreadable field, a
  contradiction between the printed total and the line items, an unusual layout.
"""

_MAX_PDF_BYTES = 30 * 1024 * 1024  # request cap is 32 MB; leave headroom for the prompt


class ExtractionUnavailable(RuntimeError):
    """Raised when the LLM reader cannot run, so the caller can fall back."""


def _user_content(path: Path) -> list[dict]:
    """Build the message content: the document itself, plus its text layer."""
    content: list[dict] = []
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        raw = path.read_bytes()
        if len(raw) > _MAX_PDF_BYTES:
            raise ExtractionUnavailable(f"{path.name} is larger than the 30 MB document limit.")
        content.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(raw).decode("ascii"),
            },
        })
    elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        media_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else f"image/{suffix.lstrip('.')}"
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
            },
        })
    else:
        text = document_text(path)
        if not text.strip():
            raise ExtractionUnavailable(f"{path.name} has no readable content.")
        content.append({"type": "text", "text": f"Document contents:\n\n{text}"})

    content.append({
        "type": "text",
        "text": (
            "Extract every field of the schema from this document. Return null for "
            "anything not printed on it."
        ),
    })
    return content


def extract(path: Path) -> ExtractedInvoice:
    """Read one document. Raises ExtractionUnavailable so callers can fall back."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ExtractionUnavailable("The anthropic package is not installed.") from exc

    # A key set in .env never reaches os.environ, so pass it explicitly. Left as
    # None, the SDK falls back to its own resolution - environment variable,
    # auth token, or a stored `ant auth login` profile.
    client = anthropic.Anthropic(
        api_key=setting("ANTHROPIC_API_KEY") or None,
        auth_token=setting("ANTHROPIC_AUTH_TOKEN") or None,
    )

    try:
        response = client.messages.parse(
            model=extraction_model(),
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_content(path)}],
            output_format=ExtractedInvoice,
        )
    except anthropic.AuthenticationError as exc:
        raise ExtractionUnavailable("The configured ANTHROPIC_API_KEY was rejected.") from exc
    except anthropic.RateLimitError as exc:
        raise ExtractionUnavailable("Rate limited by the Claude API; try this document again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionUnavailable(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionUnavailable("Could not reach the Claude API - check network access.") from exc

    if response.stop_reason == "refusal":
        raise ExtractionUnavailable("The reader declined to process this document.")

    parsed = response.parsed_output
    if parsed is None:
        raise ExtractionUnavailable("The reader returned no structured output for this document.")
    return parsed
