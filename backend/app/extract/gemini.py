"""Read a document with Google Gemini.

The alternative to `llm.py`. Same contract: take a file, return an
`ExtractedInvoice`, raise `ExtractionUnavailable` when it cannot, so the
pipeline can fall back to the offline reader without knowing which provider
was in play.

Gemini accepts a PDF inline and constrains its output to a Pydantic schema, so
the mapping from the Claude reader is close to one-for-one. The prompt is
shared between the two providers deliberately: the instruction not to decide
the tax treatment is the important part, and it should not drift between them.
"""

from __future__ import annotations

from pathlib import Path

from ..config import gemini_model, setting
from ..models import ExtractedInvoice
from .llm import SYSTEM_PROMPT, ExtractionUnavailable
from .pdftext import document_text

# Gemini bills per request against a free-tier quota, so the same 30 MB ceiling
# the Claude reader uses applies here too.
_MAX_BYTES = 30 * 1024 * 1024

_IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


def _parts(path: Path, types) -> list:
    """The document itself, then the instruction to read it."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        raw = path.read_bytes()
        if len(raw) > _MAX_BYTES:
            raise ExtractionUnavailable(f"{path.name} is larger than the 30 MB document limit.")
        document = types.Part.from_bytes(data=raw, mime_type="application/pdf")
    elif suffix in _IMAGE_TYPES:
        raw = path.read_bytes()
        if len(raw) > _MAX_BYTES:
            raise ExtractionUnavailable(f"{path.name} is larger than the 30 MB document limit.")
        document = types.Part.from_bytes(data=raw, mime_type=_IMAGE_TYPES[suffix])
    else:
        text = document_text(path)
        if not text.strip():
            raise ExtractionUnavailable(f"{path.name} has no readable content.")
        document = types.Part.from_text(text=f"Document contents:\n\n{text}")

    instruction = types.Part.from_text(
        text=(
            "Extract every field of the schema from this document. "
            "Return null for anything not printed on it."
        )
    )
    return [document, instruction]


def extract(path: Path) -> ExtractedInvoice:
    """Read one document. Raises ExtractionUnavailable so callers can fall back."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ExtractionUnavailable(
            "The google-genai package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    key = setting("GEMINI_API_KEY") or setting("GOOGLE_API_KEY")
    if not key:
        raise ExtractionUnavailable("No GEMINI_API_KEY configured.")

    client = genai.Client(api_key=key)

    try:
        response = client.models.generate_content(
            model=gemini_model(),
            contents=_parts(path, types),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=ExtractedInvoice,
                # The reader transcribes; it does not compose. Low temperature
                # keeps it from smoothing an odd-looking figure into a tidy one.
                temperature=0,
            ),
        )
    except ExtractionUnavailable:
        raise
    except Exception as exc:
        raise ExtractionUnavailable(_explain(exc)) from exc

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, ExtractedInvoice):
        return parsed
    if isinstance(parsed, dict):
        return ExtractedInvoice.model_validate(parsed)

    # The schema should guarantee JSON, but a blocked or empty response comes
    # back with no parsed payload at all rather than as an exception.
    text = getattr(response, "text", None)
    if not text:
        raise ExtractionUnavailable(
            "Gemini returned no content for this document - it may have been blocked or truncated."
        )
    try:
        return ExtractedInvoice.model_validate_json(text)
    except Exception as exc:
        raise ExtractionUnavailable(f"Gemini returned output that did not match the schema: {exc}") from exc


def _explain(exc: Exception) -> str:
    """Turn an SDK error into something a reviewer can act on."""
    message = str(exc)
    lowered = message.lower()

    if "api key not valid" in lowered or "api_key_invalid" in lowered:
        return "The configured GEMINI_API_KEY was rejected. Check it in backend/.env."
    if "quota" in lowered or "resource_exhausted" in lowered or "429" in message:
        return (
            "Gemini's free-tier quota is exhausted for now. It resets on its own - "
            "try this document again shortly, or move to a paid tier."
        )
    if "permission" in lowered or "403" in message:
        return "Gemini refused the request. Check that the Gemini API is enabled for this key's project."
    if "deadline" in lowered or "timeout" in lowered:
        return "Gemini did not respond in time; try this document again."
    return f"Gemini error: {message}"
