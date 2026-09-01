"""The six-stage pipeline, wired end to end.

capture -> classify -> extract -> apply GST rules -> validate -> write & notify

Stages 2 and 4 live in `gst/rules.py`, stage 3 in `extract/`, stage 5 in
`gst/validate.py`, and stage 6 in `workbook.py`. This module is the sequence
itself, plus the decision about whether a document can post straight through or
has to stop at Quick Review.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import ClassVar

from . import history, runtime, store, uploads, workbook
from . import period as periods
from .config import (
    ARCHIVE_DIR,
    INCOMING_DIR,
    IRA_INNOVATIONS,
    approval_mode,
    ensure_dirs,
    extraction_provider,
    has_credentials,
)
from .extract import gemini as gemini_extractor
from .extract import heuristic
from .extract import llm as llm_extractor

# Providers are interchangeable at extract(path) -> ExtractedInvoice.
_READERS = {"claude": llm_extractor, "gemini": gemini_extractor}
from .extract.pdftext import document_text
from .extract.split import find_segments, write_segment
from .gst import rules
from .gst.validate import ValidationResult, check_duplicate, reconcile_tax, validate_gstin
from .models import DocStatus, DocumentType, ExtractedInvoice, GstTreatment

SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".csv"}


class DuplicateUpload(ValueError):
    """This exact file is already in the queue.

    Separate from the other ValueErrors stage() raises, because it is not a bad
    file - it is a file the system already has, and the caller may legitimately
    want to insist.
    """

    def __init__(self, message: str, existing: list[dict]):
        super().__init__(message)
        self.existing = existing


class Stage:
    """Where a document has got to, for the progress display.

    Distinct from `status`, which is what a *reviewer* needs to act on. Stage is
    the machine's own progress through the pipeline, so a person watching an
    upload sees movement instead of an undifferentiated spinner.
    """

    UPLOADED = "uploaded"
    READING = "reading"
    EXTRACTED = "extracted"
    CHECKED = "checked"
    DONE = "done"
    POSTED = "posted"
    FAILED = "failed"

    # In order, with the words shown on the Processing screen. A tuple because
    # nothing should be appending stages at runtime.
    SEQUENCE: ClassVar[tuple[tuple[str, str], ...]] = (
        (UPLOADED, "Invoice received"),
        (READING, "Reading the document"),
        (EXTRACTED, "Invoice details extracted"),
        (CHECKED, "GST rules applied and checked"),
        (DONE, "Ready for review"),
        (POSTED, "Written to the workbook"),
    )


# --------------------------------------------------------------------------- #
# Stage 1: capture
# --------------------------------------------------------------------------- #

def _already_held(digest: str) -> list[dict]:
    """Documents in the queue that came from these exact bytes.

    Posted documents count. Re-uploading something already written to the
    workbook is the mistake most worth catching, not the one to wave through.
    """
    return [d for d in store.all_documents() if d.get("source_hash") == digest]


def stage(data: bytes, filename: str, source: str = "upload",
          allow_duplicate: bool = False) -> list[dict]:
    """Store an arriving file and register every invoice in it, without reading.

    One file is not necessarily one invoice: billing software exports a month's
    invoices as a single print run. Each invoice found becomes its own document,
    with its own pages, so none of them is silently dropped.

    Splitting is local and quick; reading is a model call per invoice. Keeping
    them apart lets an upload answer as soon as the documents exist, and read
    them afterwards, rather than holding the request open for the length of
    every call in the batch.
    """
    ensure_dirs()
    safe_name = Path(filename).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"{safe_name}: unsupported file type. Accepted: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )

    # The extension says what it is called; the bytes say what it is. A file
    # that disagrees with itself is refused here rather than failing later in a
    # parser, where the message would be about a broken PDF rather than about
    # the file not being one.
    try:
        uploads.verify(data, safe_name, suffix)
    except uploads.RejectedUpload as exc:
        raise ValueError(str(exc)) from exc

    # Checked before anything is stored or read: an accidental second upload of
    # the same batch should cost nothing, not a model call per invoice in it.
    digest = hashlib.sha256(data).hexdigest()
    if not allow_duplicate:
        existing = _already_held(digest)
        if existing:
            when = (existing[0].get("received_at") or "")[:16].replace("T", " ")
            raise DuplicateUpload(
                f"{safe_name}: already uploaded"
                + (f" on {when}" if when else "")
                + f" ({len(existing)} document{'s' if len(existing) > 1 else ''} from it are "
                  f"already in the queue). Upload it again only if you mean to.",
                existing,
            )

    staged = INCOMING_DIR / f"staged__{store.new_id()}__{safe_name}"
    staged.write_bytes(data)

    try:
        segments = find_segments(staged)
    except Exception:
        segments = []

    # Nothing to split: keep the file exactly as it arrived.
    if len(segments) <= 1:
        doc_id = store.new_id()
        stored = INCOMING_DIR / f"{doc_id}__{safe_name}"
        staged.replace(stored)
        return [store.add({
            "id": doc_id,
            "filename": safe_name,
            "stored_path": str(stored),
            "source": source,
            "status": DocStatus.NEW.value,
            "stage": Stage.UPLOADED,
            # The file this came from, so an identical re-upload is recognised
            # before it costs anything.
            "source_hash": digest,
        })]

    stem = Path(safe_name).stem
    records: list[dict] = []
    try:
        for position, segment in enumerate(segments, start=1):
            doc_id = store.new_id()
            part_name = f"{stem} [{segment.label}]{suffix}"
            stored = INCOMING_DIR / f"{doc_id}__{part_name}"
            write_segment(staged, segment, stored)
            records.append(store.add({
                "id": doc_id,
                "filename": part_name,
                "stored_path": str(stored),
                "source": source,
                "status": DocStatus.NEW.value,
                # Provenance, so a reviewer can see where this invoice came from.
                "source_document": safe_name,
                "page_label": segment.label,
                "position": position,
                "of": len(segments),
            }))
    finally:
        staged.unlink(missing_ok=True)

    return records


def capture(data: bytes, filename: str, source: str = "upload") -> list[dict]:
    """Stage an arriving file and read every invoice in it, in one call.

    The synchronous path, used by the watch folder and by the tests. The HTTP
    upload route stages and reads separately so it can answer immediately.
    """
    return [process(record["id"]) for record in stage(data, filename, source)]


def process_many(doc_ids: list[str]) -> None:
    """Read a batch of already-staged documents.

    This runs detached from any request, so nothing here may raise: a document
    that cannot be read has to be recorded as failed *on the document*, where a
    reviewer will see it, rather than disappearing into a traceback on a
    background task nobody is watching.
    """
    for doc_id in doc_ids:
        try:
            process(doc_id)
        except Exception as exc:
            store.update(
                doc_id,
                {
                    "status": DocStatus.FAILED.value,
                    "stage": Stage.FAILED,
                    "error": f"Could not read this document: {exc}",
                    "failure_reason": ("Something went wrong while reading this document. "
                                       "Try again, or upload a different copy of it."),
                },
                event="read_failed",
            )


def capture_path(path: Path, source: str = "scan") -> list[dict]:
    return capture(path.read_bytes(), path.name, source=source)


# --------------------------------------------------------------------------- #
# Stages 2-5
# --------------------------------------------------------------------------- #

def _read_document(path: Path) -> tuple[ExtractedInvoice, str, str | None]:
    """Read one document with the configured provider.

    Returns (extracted, reader, note). `reader` is the reader that ACTUALLY
    ran, not the one configured - a provider that is configured but refuses
    (no credit, exhausted quota, rejected key) falls through to the offline
    reader, and the note says why so nobody has to guess.
    """
    provider = extraction_provider()
    reader = _READERS.get(provider)

    if reader is None:
        return heuristic.extract(path), "heuristic", f"Unknown extraction provider {provider!r}."

    if not has_credentials():
        return heuristic.extract(path), "heuristic", _no_credentials_note(provider)

    try:
        return reader.extract(path), provider, None
    except llm_extractor.ExtractionUnavailable as exc:
        return heuristic.extract(path), "heuristic", str(exc)
    except Exception as exc:  # unexpected: still capture rather than lose the document
        return heuristic.extract(path), "heuristic", f"Reader failed: {exc}"


def _provider_key_name() -> str:
    """The env var the ACTIVE provider needs. Naming the wrong one sends
    someone to a key that will not help them."""
    return "GEMINI_API_KEY" if extraction_provider() == "gemini" else "ANTHROPIC_API_KEY"


def _provider_label() -> str:
    return "a Gemini API key" if extraction_provider() == "gemini" else "a Claude API key"


def _no_credentials_note(provider: str) -> str:
    if provider == "gemini":
        return "No GEMINI_API_KEY configured - add it to backend/.env for a full read."
    return ("No Claude credentials configured - add ANTHROPIC_API_KEY to backend/.env "
            "for a full read.")


def evaluate(doc: ExtractedInvoice, *, exclude_key: tuple[str, str | None] | None = None) -> tuple[GstTreatment, ValidationResult]:
    """Apply GST rules, then validate the result. Used by both intake and confirm."""
    treatment = rules.apply_gst(doc, IRA_INNOVATIONS)
    result = ValidationResult()

    if treatment.document_type is DocumentType.SALES:
        validate_gstin(result, doc.supplier_gstin or IRA_INNOVATIONS.gstin, "Supplier", required=True)
        validate_gstin(result, doc.recipient_gstin, "Customer", required=False)
    else:
        validate_gstin(result, doc.supplier_gstin, "Supplier", required=True)

    if not treatment.place_of_supply_code:
        result.add(
            "place_of_supply_unknown",
            "Place of supply could not be determined, so the CGST/SGST vs IGST split is a guess.",
        )
    elif not treatment.supplier_state_code:
        result.add("supplier_state_unknown", "Supplier state could not be determined from the GSTIN.")

    # Fields the register row cannot be written without. The invoice date is
    # doubly required: it also decides which return period this invoice belongs
    # to, and therefore which workbook it is written into.
    invoice_date = workbook.parse_date(doc.invoice_date)
    if not doc.invoice_date or invoice_date is None:
        result.add(
            "invoice_date_missing",
            "Invoice date is missing or could not be read as a date, so the return period "
            "this invoice belongs to cannot be determined.",
        )
    if not (treatment.counterparty_name or "").strip():
        result.add("counterparty_missing", "The other party's name could not be read off the document.")

    reconcile_tax(
        result,
        stated_total_tax=rules.stated_total_tax(doc),
        computed_total_tax=treatment.total_tax,
        taxable_value=treatment.taxable_value,
        rate=treatment.rate,
    )

    period = period_for(doc)
    existing = workbook.posted_keys(treatment.document_type, period) if period else []
    if exclude_key is not None:
        existing = [key for key in existing if key != exclude_key]
    check_duplicate(
        result,
        invoice_no=doc.invoice_number,
        party_gstin=treatment.counterparty_gstin,
        existing=existing,
    )

    if "Neither party matched" in treatment.classification_reason or "defaulted" in treatment.classification_reason:
        result.add("classification_uncertain", treatment.classification_reason)

    return treatment, result


def period_for(doc: ExtractedInvoice) -> str | None:
    """The return period an invoice belongs to, read off its own date.

    Nothing configures this. A GST return is monthly and an invoice belongs to
    the month it was raised in, so a July invoice files against July whatever
    else is in the queue beside it.
    """
    invoice_date = workbook.parse_date(doc.invoice_date)
    return periods.period_of(invoice_date) if invoice_date else None


def _status_for(result: ValidationResult) -> DocStatus:
    """Where a document lands once it has been read and checked."""
    if not result.ok:
        return DocStatus.NEEDS_REVIEW
    if approval_mode() == "every_row":
        return DocStatus.NEEDS_REVIEW
    return DocStatus.READY


def process(doc_id: str) -> dict:
    """Read and evaluate a captured document, leaving it ready or flagged."""
    record = store.get(doc_id)
    if record is None:
        raise KeyError(doc_id)

    path = Path(record["stored_path"])
    if not path.exists():
        return store.update(doc_id, {
            "status": DocStatus.FAILED.value,
            "stage": Stage.FAILED,
            "error": "The stored file is missing.",
        }, event="read_failed")

    # Published before the slow part, so the Processing screen can show that
    # reading has begun rather than sitting on "received" for a minute.
    store.update(doc_id, {"stage": Stage.READING})

    extracted, reader, reader_note = _read_document(path)

    # Record what happened together with the configuration it happened under,
    # so the header can report the reader that is really running without
    # quoting a note from before the last key change.
    runtime.record_read(extraction_provider(), has_credentials(), reader, reader_note)

    store.update(doc_id, {"stage": Stage.EXTRACTED})

    treatment, result = evaluate(extracted)
    store.update(doc_id, {"stage": Stage.CHECKED})

    # A document with no text layer is the one case where running offline is a
    # problem with *this* document rather than a background fact - the offline
    # reader has nothing to parse. Say so here, where it matters, instead of
    # warning about it on every document in advance.
    if reader == "heuristic" and not document_text(path).strip():
        # Every other failure on this document is a consequence of the same one
        # cause, so report the cause alone rather than its symptoms.
        result = ValidationResult()
        result.add(
            "no_text_layer",
            f"{record['filename']} has no text layer - it is a scan or a photo, so the "
            f"offline reader has nothing to parse. Reading it needs {_provider_label()}: "
            f"add {_provider_key_name()} to backend/.env, then press Read again.",
        )

    # The same invoice can arrive as a different file - emailed, then scanned,
    # then re-exported - so the hash check at the door cannot see it. This one
    # looks at what the document turned out to be.
    #
    # A warning, not a blocker: the register check at post time is what protects
    # the workbook, and two queue entries are not yet a filing error. But
    # someone working through the queue should not have to notice on their own.
    twin = _queue_twin(doc_id, extracted, treatment)
    if twin:
        result.add(
            "duplicate_in_queue",
            f"Invoice {extracted.invoice_number} is already in the queue "
            f"({twin.get('filename')}). Posting both would be rejected as a duplicate.",
            severity="warning",
        )

    # What the previous invoices from this supplier say. Deliberately after the
    # read and never fed into it: handing the reader a plausible prior invites
    # it to reconcile the document against history rather than report what is
    # printed, and an invented GSTIN that matches the last eleven invoices is
    # the hardest kind of wrong to notice.
    supplier = history.match(treatment.counterparty_name, treatment.counterparty_gstin)
    for code, message in history.anomalies(
        treatment.counterparty_name, treatment.counterparty_gstin, treatment.rate
    ):
        result.add(code, message, severity="warning")

    status = _status_for(result)

    return store.update(doc_id, {
        "status": status.value,
        "supplier_history": supplier.as_dict() if supplier else None,
        "stage": Stage.DONE,
        "reader": reader,
        "reader_note": reader_note,
        "period": period_for(extracted),
        "extracted": extracted.model_dump(mode="json"),
        "treatment": treatment.model_dump(mode="json"),
        "issues": result.as_dicts(),
        "failure_reason": _failure_reason(result, reader, record["filename"]),
        "error": None,
    }, event=f"read_by_{reader}")


def _queue_twin(doc_id: str, doc: ExtractedInvoice, treatment: GstTreatment) -> dict | None:
    """Another document in the queue claiming to be the same invoice."""
    number = (doc.invoice_number or "").strip().casefold()
    if not number:
        return None
    party = (treatment.counterparty_gstin or "").strip().upper()

    for other in store.all_documents():
        if other["id"] == doc_id:
            continue
        extracted = other.get("extracted") or {}
        if (extracted.get("invoice_number") or "").strip().casefold() != number:
            continue
        # An invoice number is only unique per party, so a bare number match
        # from a different supplier is a coincidence, not a duplicate.
        seen_party = ((other.get("treatment") or {}).get("counterparty_gstin") or "").strip().upper()
        if not party or not seen_party or seen_party == party:
            return other
    return None


def _failure_reason(result: ValidationResult, reader: str, filename: str) -> str | None:
    """One sentence a person can act on, or None if nothing is blocking.

    The issues list is precise and complete, which is right for a reviewer
    working through a document and wrong for a screen that has to say, in one
    line, why this did not go through. This picks the cause rather than listing
    the symptoms.
    """
    blocking = result.blocking
    if not blocking:
        return None

    codes = {issue.code for issue in blocking}
    if "no_text_layer" in codes:
        return (f"{filename} is a scan or a photograph with no text in it, so the offline "
                f"reader had nothing to work from. Reading it needs the Claude reader.")
    if "invoice_date_missing" in codes:
        return ("No invoice date could be read, and the date decides which return period "
                "this belongs to.")
    if "invoice_no_missing" in codes:
        return "No invoice number could be read off the document."
    if "duplicate_invoice" in codes:
        return "This invoice number is already posted in this return period."
    if codes & {"gstin_checksum", "gstin_format", "gstin_state", "gstin_missing"}:
        return "The GSTIN on this document did not check out - likely a misread character."
    if "tax_mismatch" in codes:
        return "The tax printed on the document does not match the tax its own figures imply."
    if "taxable_value" in codes:
        return "No taxable value could be read off the document."
    return blocking[0].message


# --------------------------------------------------------------------------- #
# Stage 6: write and notify
# --------------------------------------------------------------------------- #

def revise(doc_id: str, edits: dict, actor: dict | None = None) -> dict:
    """Apply a reviewer's corrections and re-run rules and validation."""
    record = store.get(doc_id)
    if record is None:
        raise KeyError(doc_id)

    merged = dict(record.get("extracted") or {})
    merged.update({k: v for k, v in edits.items() if k in ExtractedInvoice.model_fields})
    doc = ExtractedInvoice.model_validate(merged)
    treatment, result = evaluate(doc)

    return store.update(doc_id, {
        "status": _status_for(result).value,
        "stage": Stage.DONE,
        "period": period_for(doc),
        "extracted": doc.model_dump(mode="json"),
        "treatment": treatment.model_dump(mode="json"),
        "issues": result.as_dicts(),
        "failure_reason": _failure_reason(result, record.get("reader") or "", record["filename"]),
    }, event="revised", actor=actor)


def confirm(doc_id: str, *, override: bool = False, actor: dict | None = None) -> dict:
    """Post a document to its register.

    `override` lets a reviewer post a document that still carries a blocking
    issue - the deliberate human decision the proposal's "quick human check"
    step describes. The override is recorded on the document's history.
    """
    record = store.get(doc_id)
    if record is None:
        raise KeyError(doc_id)
    if record.get("status") == DocStatus.POSTED.value:
        raise ValueError("This document is already posted.")

    doc = ExtractedInvoice.model_validate(record["extracted"])
    treatment, result = evaluate(doc)

    if not result.ok and not override:
        store.update(doc_id, {
            "status": DocStatus.NEEDS_REVIEW.value,
            "issues": result.as_dicts(),
            "treatment": treatment.model_dump(mode="json"),
            "failure_reason": _failure_reason(result, record.get("reader") or "",
                                              record["filename"]),
        }, event="post_blocked", actor=actor)
        raise ValueError("; ".join(issue.message for issue in result.blocking))

    period = period_for(doc)
    if not period:
        raise ValueError("This invoice has no readable date, so its return period is unknown.")

    meta = {
        "period": period,
        "invoice_number": doc.invoice_number,
        "invoice_date_obj": workbook.parse_date(doc.invoice_date),
        "hsn_sac": doc.hsn_sac or (doc.line_items[0].hsn_sac if doc.line_items else None),
        "quantity": doc.quantity or (doc.line_items[0].quantity if doc.line_items else None),
        "unit_rate": doc.line_items[0].unit_rate if doc.line_items else None,
    }

    sheet, row = workbook.post_row(treatment, meta, period)

    # Archive the source next to the row it produced, so the audit trail is
    # already assembled if a GST officer asks.
    archived = _archive(Path(record["stored_path"]), period, sheet, row, record["filename"])

    return store.update(doc_id, {
        "status": DocStatus.POSTED.value,
        "stage": Stage.POSTED,
        "issues": result.as_dicts(),
        "treatment": treatment.model_dump(mode="json"),
        "sheet": sheet,
        "row": row,
        "period": period,
        "archived_path": str(archived) if archived else None,
        "posted_override": bool(override and not result.ok),
        "failure_reason": None,
        # Who stands behind this row. An override in particular is a deliberate
        # human decision to file something that failed a check, and an auditor
        # asking "who approved this?" deserves an answer.
        "posted_by": (actor or {}).get("email"),
        "posted_by_name": (actor or {}).get("name"),
    }, event="posted_with_override" if (override and not result.ok) else "posted", actor=actor)


def unpost(doc_id: str, actor: dict | None = None) -> dict:
    """Undo a posting: clear the workbook row and return the document to review."""
    record = store.get(doc_id)
    if record is None:
        raise KeyError(doc_id)
    if record.get("status") != DocStatus.POSTED.value or not record.get("sheet"):
        raise ValueError("This document is not posted.")

    workbook.unpost_row(record["sheet"], int(record["row"]), record["period"])

    # The archive is an audit trail for posted rows. A cleared row has no trail.
    archived = record.get("archived_path")
    if archived:
        Path(archived).unlink(missing_ok=True)

    return store.update(doc_id, {
        "status": DocStatus.NEEDS_REVIEW.value,
        "stage": Stage.DONE,
        "sheet": None,
        "row": None,
        "archived_path": None,
        "posted_by": None,
        "posted_by_name": None,
    }, event="unposted", actor=actor)


def _archive(source: Path, period: str, sheet: str, row: int, filename: str) -> Path | None:
    """Keep the source beside the row it produced, filed under its period."""
    if not source.exists():
        return None
    folder = ARCHIVE_DIR / period / sheet.replace(" ", "-")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"row{row:04d}__{filename}"
    shutil.copy2(source, target)
    return target
