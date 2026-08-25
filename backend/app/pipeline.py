"""The six-stage pipeline, wired end to end.

capture -> classify -> extract -> apply GST rules -> validate -> write & notify

Stages 2 and 4 live in `gst/rules.py`, stage 3 in `extract/`, stage 5 in
`gst/validate.py`, and stage 6 in `workbook.py`. This module is the sequence
itself, plus the decision about whether a document can post straight through or
has to stop at Quick Review.
"""

from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path

from . import store, workbook
from . import period as periods
from .config import (
    ARCHIVE_DIR, INCOMING_DIR, IRA_INNOVATIONS, approval_mode, ensure_dirs, has_credentials,
)
from .extract import heuristic
from .extract import llm as llm_extractor
from .extract.pdftext import document_text
from .extract.split import find_segments, write_segment
from .gst import rules
from .gst.validate import ValidationResult, check_duplicate, reconcile_tax, validate_gstin
from .models import DocStatus, DocumentType, ExtractedInvoice, GstTreatment

SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".csv"}


# --------------------------------------------------------------------------- #
# Stage 1: capture
# --------------------------------------------------------------------------- #

def stage(data: bytes, filename: str, source: str = "upload") -> list[dict]:
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
        except Exception as exc:  # noqa: BLE001 - a background task must not die
            store.update(
                doc_id,
                {"status": DocStatus.FAILED.value, "error": f"Could not read this document: {exc}"},
                event="read_failed",
            )


def capture_path(path: Path, source: str = "scan") -> list[dict]:
    return capture(path.read_bytes(), path.name, source=source)


# --------------------------------------------------------------------------- #
# Stages 2-5
# --------------------------------------------------------------------------- #

def _read_document(path: Path) -> tuple[ExtractedInvoice, str, str | None]:
    """Run the LLM reader, falling back to the offline reader if it cannot."""
    if has_credentials():
        try:
            return llm_extractor.extract(path), "claude", None
        except llm_extractor.ExtractionUnavailable as exc:
            return heuristic.extract(path), "heuristic", str(exc)
        except Exception as exc:  # unexpected: still capture rather than lose the document
            return heuristic.extract(path), "heuristic", f"Reader failed: {exc}"
    return heuristic.extract(path), "heuristic", (
        "No Claude credentials configured - add ANTHROPIC_API_KEY to .env for a full read."
    )


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
        return store.update(doc_id, {"status": DocStatus.FAILED.value,
                                     "error": "The stored file is missing."}, event="read_failed")

    extracted, reader, reader_note = _read_document(path)
    treatment, result = evaluate(extracted)

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
            f"{record['filename']} has no text layer - it is a scan or a photo, so the offline "
            f"reader has nothing to parse. Reading it needs a Claude API key: add "
            f"ANTHROPIC_API_KEY to .env, then press Read again.",
        )

    status = _status_for(result)

    return store.update(doc_id, {
        "status": status.value,
        "reader": reader,
        "reader_note": reader_note,
        "period": period_for(extracted),
        "extracted": extracted.model_dump(mode="json"),
        "treatment": treatment.model_dump(mode="json"),
        "issues": result.as_dicts(),
        "error": None,
    }, event=f"read_by_{reader}")


# --------------------------------------------------------------------------- #
# Stage 6: write and notify
# --------------------------------------------------------------------------- #

def revise(doc_id: str, edits: dict) -> dict:
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
        "period": period_for(doc),
        "extracted": doc.model_dump(mode="json"),
        "treatment": treatment.model_dump(mode="json"),
        "issues": result.as_dicts(),
    }, event="revised")


def confirm(doc_id: str, *, override: bool = False) -> dict:
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
        }, event="post_blocked")
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
        "issues": result.as_dicts(),
        "treatment": treatment.model_dump(mode="json"),
        "sheet": sheet,
        "row": row,
        "period": period,
        "archived_path": str(archived) if archived else None,
        "posted_override": bool(override and not result.ok),
    }, event="posted_with_override" if (override and not result.ok) else "posted")


def unpost(doc_id: str) -> dict:
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
        "sheet": None,
        "row": None,
        "archived_path": None,
    }, event="unposted")


def _archive(source: Path, period: str, sheet: str, row: int, filename: str) -> Path | None:
    """Keep the source beside the row it produced, filed under its period."""
    if not source.exists():
        return None
    folder = ARCHIVE_DIR / period / sheet.replace(" ", "-")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"row{row:04d}__{filename}"
    shutil.copy2(source, target)
    return target
