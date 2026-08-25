"""The backend service: a JSON API over the invoice pipeline.

This process serves no HTML. The frontend is a separate server on its own port
that calls this API cross-origin, so the two can be restarted, scaled, deployed
and debugged independently of one another.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import pipeline, store, workbook
from . import period as periods
from .config import (
    DATA_DIR, approval_mode, credential_source, ensure_dirs, extraction_model, frontend_origins,
    has_credentials,
)
from .models import DocStatus, DocumentType

DROP_DIR = DATA_DIR / "dropbox"

app = FastAPI(
    title="Ira Innovations - GST Invoice Automation API",
    description="Invoice to filed return, without the manual typing.",
    version="2.0.0",
)

# The frontend runs on its own origin, so every browser call here is
# cross-origin and needs to be allowed explicitly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins(),
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # No cookies are used; the API is stateless.
    allow_credentials=False,
)


@app.get("/api/health")
def health() -> dict:
    """Liveness probe, so the frontend can tell you when the API is not up."""
    return {"status": "ok", "service": "gst-automation-backend"}


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    DROP_DIR.mkdir(parents=True, exist_ok=True)
    # The master workbook's own period is always available; others are created
    # on demand as invoices for them arrive.
    workbook.ensure_working_copy(workbook.master_period())


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #

@app.get("/api/info")
def info() -> dict:
    documents = store.all_documents()
    counts = {status.value: 0 for status in DocStatus}
    for doc in documents:
        counts[doc.get("status", DocStatus.NEW.value)] = counts.get(doc.get("status"), 0) + 1

    # Every period the queue refers to, whether or not a workbook exists yet.
    queued = {d["period"] for d in documents if d.get("period")}
    known = sorted(set(workbook.available_periods()) | queued, key=periods.sort_key, reverse=True)
    return {
        **workbook.workbook_info(),
        "reader": "claude" if has_credentials() else "heuristic",
        "model": extraction_model() if has_credentials() else None,
        "credential_source": credential_source(),
        "approval_mode": approval_mode(),
        "counts": counts,
        "periods": known,
        "drop_folder": str(DROP_DIR),
    }


# --------------------------------------------------------------------------- #
# Screen 1: Invoice Inbox
# --------------------------------------------------------------------------- #

@app.get("/api/documents")
def list_documents() -> list[dict]:
    return store.all_documents()


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str) -> dict:
    doc = store.get(doc_id)
    if doc is None:
        raise HTTPException(404, "No such document.")
    return doc


@app.post("/api/documents")
async def upload_documents(files: list[UploadFile] = File(...)) -> dict:
    """Capture one or more invoices arriving by upload."""
    created, errors = [], []
    for upload in files:
        try:
            created.extend(pipeline.capture(await upload.read(), upload.filename or "document.pdf", "upload"))
        except ValueError as exc:
            errors.append(str(exc))
    if not created and errors:
        raise HTTPException(400, "; ".join(errors))
    return {"captured": created, "errors": errors}


@app.post("/api/ingest/folder")
def ingest_folder() -> dict:
    """Pick up anything dropped in data/dropbox.

    This stands in for the email and scanner channels: point a mail rule or a
    scanner's output at this folder and every new file enters the pipeline.
    """
    DROP_DIR.mkdir(parents=True, exist_ok=True)
    seen = {Path(d["filename"]).name for d in store.all_documents()}
    captured, errors = [], []
    for path in sorted(DROP_DIR.iterdir()):
        if not path.is_file() or path.name in seen:
            continue
        try:
            captured.extend(pipeline.capture_path(path, source="scan"))
        except ValueError as exc:
            errors.append(str(exc))
    return {"captured": captured, "errors": errors}


@app.post("/api/documents/{doc_id}/reprocess")
def reprocess(doc_id: str) -> dict:
    try:
        return pipeline.process(doc_id)
    except KeyError:
        raise HTTPException(404, "No such document.")


@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: str) -> dict:
    doc = store.get(doc_id)
    if doc is None:
        raise HTTPException(404, "No such document.")
    if doc.get("status") == DocStatus.POSTED.value:
        raise HTTPException(409, "Un-post this document before removing it.")
    store.delete(doc_id)
    return {"removed": doc_id}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str):
    """Serve the original document for the Quick Review pane."""
    doc = store.get(doc_id)
    if doc is None:
        raise HTTPException(404, "No such document.")
    path = Path(doc["stored_path"])
    if not path.exists():
        raise HTTPException(404, "The stored file is missing.")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=doc["filename"],
                        content_disposition_type="inline")


# --------------------------------------------------------------------------- #
# Screen 2: Quick Review
# --------------------------------------------------------------------------- #

class Revision(BaseModel):
    """A reviewer's corrections. Only extraction fields are accepted - the tax
    treatment is always re-derived, never submitted from the browser."""

    model_config = {"extra": "allow"}


@app.patch("/api/documents/{doc_id}")
def revise_document(doc_id: str, revision: Revision) -> dict:
    try:
        return pipeline.revise(doc_id, revision.model_dump())
    except KeyError:
        raise HTTPException(404, "No such document.")


@app.post("/api/documents/{doc_id}/confirm")
def confirm_document(doc_id: str, override: bool = False) -> dict:
    try:
        return pipeline.confirm(doc_id, override=override)
    except KeyError:
        raise HTTPException(404, "No such document.")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/documents/{doc_id}/unpost")
def unpost_document(doc_id: str) -> dict:
    try:
        return pipeline.unpost(doc_id)
    except KeyError:
        raise HTTPException(404, "No such document.")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# --------------------------------------------------------------------------- #
# Screen 3: Registers
# --------------------------------------------------------------------------- #

@app.get("/api/registers/{register}")
def register(register: str, period: str | None = None) -> dict:
    try:
        doc_type = DocumentType(register)
    except ValueError:
        raise HTTPException(404, f"Unknown register {register!r}.")
    period = _resolve_period(period)
    return {
        "register": doc_type.value,
        "sheet": doc_type.sheet,
        "period": period,
        "columns": workbook.register_columns(doc_type),
        "rows": workbook.read_register(doc_type, period),
    }


# --------------------------------------------------------------------------- #
# Screen 4: Tax Payable
# --------------------------------------------------------------------------- #

@app.get("/api/tax-payable")
def tax_payable(period: str | None = None) -> dict:
    period = _resolve_period(period)
    summary = workbook.tax_payable_summary(period).model_dump()
    # A period created for a later month starts with no opening credit; that is
    # a figure only the previous return can supply.
    summary["opening_credit_unset"] = workbook.opening_credit_is_unset(period)
    return summary


@app.get("/api/workbook/download")
def download_workbook(period: str | None = None):
    period = _resolve_period(period)
    workbook.ensure_working_copy(period)
    return FileResponse(
        workbook.workbook_path(period),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"Ira Innovations GST {period}.xlsx",
    )


@app.post("/api/workbook/reset")
def reset_workbook(period: str | None = None) -> dict:
    """Discard posted rows. One period, or everything if none is named."""
    workbook.reset_working_copy(period)
    if period is None:
        store.clear()
    return {"reset": period or "all"}


def _resolve_period(period: str | None) -> str:
    """Validate a requested period, defaulting to the most recent one held."""
    if period:
        if not periods.is_valid(period):
            raise HTTPException(400, f"{period!r} is not a return period, e.g. 'Jul-26'.")
        return period
    known = workbook.available_periods()
    return known[0] if known else workbook.master_period()
