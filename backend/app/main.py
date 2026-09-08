"""The backend service: a JSON API over the invoice pipeline.

This process serves no HTML. The frontend is a separate server on its own port
that calls this API cross-origin, so the two can be restarted, scaled, deployed
and debugged independently of one another.

Routes state their own access requirement with a dependency rather than relying
on a blanket middleware, because "everything is protected except this list" is
the shape that eventually leaks a route.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import threading
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import (
    accounts,
    appsettings,
    auth,
    db,
    history,
    pipeline,
    runtime,
    singleton,
    store,
    workbook,
)
from . import period as periods
from .auth import require_admin, require_user
from .config import (
    DATA_DIR,
    api_key,
    app_url,
    approval_mode,
    credential_source,
    ensure_dirs,
    extraction_model,
    extraction_provider,
    frontend_origins,
    gemini_tier,
    has_credentials,
    lock_path,
    max_upload_bytes,
    training_risk,
)
from .models import DocStatus, DocumentType

log = logging.getLogger("gst.backend")



@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Claim the data directory, prepare it, and give it up cleanly on exit."""
    ensure_dirs()

    # Before anything opens a workbook: two processes sharing this directory
    # would overwrite each other's posted rows with no error anywhere.
    singleton.acquire(lock_path())

    workbook.ensure_working_copy(workbook.master_period())
    accounts.purge_expired()
    accounts.prune_activity()
    _recover_stranded_documents()
    _announce()
    try:
        yield
    finally:
        singleton.release()


def _database_detail() -> str:
    """Which database this process is actually talking to.

    Reporting db.path() unconditionally described a SQLite file that a
    PostgreSQL deployment never opens - on the one screen someone reads while
    working out whether a deployment came up correctly. The password is
    stripped rather than the whole URL hidden, because "which server, which
    database" is exactly what that person is checking.
    """
    if db.dialect() == "sqlite":
        return str(db.path())

    parsed = urlsplit(db.url())
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    name = parsed.path.lstrip("/") or "?"
    user = f"{parsed.username}@" if parsed.username else ""
    return f"postgresql://{user}{host}{port}/{name}"


def _recover_stranded_documents() -> None:
    """Read anything a restart caught mid-flight (gap A6).

    Reading happens in a background task. If the process stops between staging
    a document and reading it, nothing retried it and nothing noticed - the
    document sat at `new` for ever while the Processing screen polled a stage
    that would never advance. Picking them up at startup closes that entirely.
    """
    stranded = [d["id"] for d in store.all_documents()
                if d.get("status") == DocStatus.NEW.value]
    if not stranded:
        return
    log.warning("%d document(s) were left unread by a restart; reading them now.",
                len(stranded))
    threading.Thread(target=pipeline.process_many, args=(stranded,), daemon=True).start()


def _announce() -> None:
    """Say out loud the things that are unsafe to get wrong silently."""
    if accounts.count_users() == 0:
        log.warning(
            "No accounts yet - the first person to sign up becomes the administrator. "
            "Do that before exposing this beyond localhost."
        )
    else:
        log.info("Accounts: %d registered.", accounts.count_users())
    if api_key():
        log.info("A service key is configured for machine access.")
    log.info("Data directory claimed: %s", DATA_DIR)


app = FastAPI(
    title="Ira Innovations - GST Invoice Automation API",
    description="Invoice to filed return, without the manual typing.",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins(),
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # The session token travels in a header, so the browser never attaches
    # ambient credentials and there is no cookie to protect.
    allow_credentials=False,
)


# --------------------------------------------------------------------------- #
# Open routes
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Security headers and unhandled errors (gaps S4, A5)
# --------------------------------------------------------------------------- #

def _apply_security_headers(headers, request: Request) -> None:
    """The header policy, in one place.

    Shared with the 500 handler deliberately. Starlette's ServerErrorMiddleware
    sits OUTSIDE the user middleware stack, so an error response is returned
    without ever passing back through the middleware below - which meant the
    one response most worth locking down was the only one going out bare.
    """
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    )
    if request.url.path.startswith("/api/"):
        # Invoice data and session state must never sit in a shared cache.
        headers.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """The last line before a stack trace reaches a browser.

    The person who hits this is an accountant mid-filing, and "Internal Server
    Error" does not tell them whether their invoice posted. So the detail goes
    to the log with a reference, and the reference is what the caller sees:
    enough to find the exact traceback, nothing about the internals of a system
    holding a company's tax records.
    """
    reference = uuid.uuid4().hex[:8]
    log.exception("unhandled error ref=%s %s %s", reference, request.method, request.url.path)
    response = JSONResponse(
        status_code=500,
        content={
            "detail": (
                "Something went wrong at our end and this request did not complete. "
                f"Nothing was written. Quote reference {reference} if you report it."
            ),
            "reference": reference,
        },
    )
    _apply_security_headers(response.headers, request)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers every response carries.

    This is a JSON API that never returns HTML, so the policy is stricter than
    a page's: nothing may frame it, nothing may be loaded from it, and a
    browser must not second-guess a declared content type.

    HSTS only over TLS, on purpose: sent over plain HTTP browsers ignore it,
    and setting it while developing on 127.0.0.1 pins a localhost HSTS entry
    that then breaks every other local project until it is cleared by hand.
    """
    response = await call_next(request)
    _apply_security_headers(response.headers, request)
    return response


@app.get("/api/health")
def health() -> dict:
    """Liveness probe: is this process alive. Never requires credentials."""
    return {"status": "ok", "service": "gst-automation-backend"}


@app.get("/api/ready")
def ready() -> JSONResponse:
    """Readiness: can this process actually do its job (gap O2).

    Liveness answered "ok" throughout every real failure this system has had,
    because the process was always running. This one touches the things that
    have to work, and returns 503 when they do not, so a monitor watching it
    reports a problem instead of a heartbeat.
    """
    checks: dict[str, dict] = {}

    try:
        with db.connect() as connection:
            connection.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
        checks["database"] = {"ok": True, "detail": _database_detail()}
    except Exception as exc:
        checks["database"] = {"ok": False, "detail": str(exc)}

    try:
        checks["master_workbook"] = {"ok": True, "detail": f"period {workbook.master_period()}"}
    except Exception as exc:
        checks["master_workbook"] = {"ok": False, "detail": str(exc)}

    data_dir = Path(DATA_DIR)
    checks["data_directory"] = {
        "ok": data_dir.exists() and os.access(data_dir, os.W_OK),
        "detail": str(data_dir),
    }

    # Not being able to reach the model is degraded, not down: the offline
    # reader still captures documents, so this must not fail a health gate.
    provider = extraction_provider()
    credentialed = has_credentials()
    last = runtime.last_read(provider, credentialed)
    fell_back = bool(last and last.get("reader") == "heuristic")

    # Absent credentials is degraded on its own, without waiting for a read to
    # prove it. Keying this off fell_back alone reported a perfectly healthy
    # reader on a fresh install with no key set - true only until the first
    # upload, and precisely when someone checking readiness wants to be told.
    checks["reader"] = {
        "ok": True,
        # Offline is the mode working as configured, not a degradation.
        "degraded": provider != "offline" and (fell_back or not credentialed),
        "detail": f"{provider}, credentials {'present' if credentialed else 'absent'}"
                  + ("" if credentialed else
                     " - documents will be captured by the offline reader until a key is set")
                  + (f"; last read fell back ({last.get('note') or 'no reason recorded'})"
                     if fell_back else ""),
    }

    healthy = all(check["ok"] for check in checks.values())
    return JSONResponse(
        {"status": "ready" if healthy else "not ready", "checks": checks},
        status_code=200 if healthy else 503,
    )


@app.get("/api/bootstrap")
def bootstrap() -> dict:
    """What the sign-in screen needs before anyone has signed in.

    Chiefly: is this a fresh install with no accounts, in which case the page
    offers to create the first one instead of asking for a password nobody has.
    """
    return {
        "needs_setup": accounts.count_users() == 0,
        "signup_mode": appsettings.get("signup_mode"),
        "company": workbook.workbook_info().get("company"),
    }


class Credentials(BaseModel):
    email: str
    password: str


class Signup(BaseModel):
    email: str
    name: str = ""
    password: str


@app.post("/api/auth/signup")
def signup(body: Signup, request: Request) -> dict:
    """Create an account from the sign-in page.

    Three cases, in order:

    - No accounts exist at all. Whoever gets here first becomes the
      administrator, because a fresh install with no way in is worse than a
      first-run signup.
    - Signup is set to `open`. A working account, signed straight in.
    - Signup is set to `approval` (the default). The account is created but
      cannot sign in until an administrator lets it in. The person is told that
      plainly rather than being handed a password that silently does nothing.

    Under `closed`, this route refuses. What it never does is decide the policy
    itself: that is a setting an administrator owns.
    """
    first_account = accounts.count_users() == 0
    mode = appsettings.get("signup_mode")

    if not first_account and mode == "closed":
        raise HTTPException(
            403, "New accounts are not open on this system. Ask an administrator to add you."
        )

    role = "admin" if first_account else "user"
    approved = first_account or mode == "open"

    try:
        user = accounts.create_user(body.email, body.name, body.password,
                                    role=role, approved=approved)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc

    if first_account:
        accounts.record(user, "signup", "first account, made administrator")
    else:
        accounts.record(user, "signup", f"self-registered ({mode})")

    if not approved:
        # No session: the account exists, and that is all it does so far.
        return {
            "pending": True,
            "user": user,
            "detail": "Your account has been created and is waiting for an administrator "
                      "to approve it. You will be able to sign in once they do.",
        }

    token = accounts.open_session(user["id"], request.headers.get("User-Agent"))
    return {"token": token, "user": user, "pending": False}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request) -> dict:
    wait = auth.retry_after(body.email, request)
    if wait:
        raise HTTPException(
            429, f"Too many failed attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    try:
        user = accounts.authenticate(body.email, body.password)
    except accounts.AccountError as exc:
        auth.note_failure(body.email, request)
        accounts.record(None, "login_failed", {"email": body.email})
        raise HTTPException(401, str(exc)) from exc

    auth.clear_failures(body.email, request)
    token = accounts.open_session(user["id"], request.headers.get("User-Agent"))
    accounts.record(user, "login")
    return {"token": token, "user": user}


class EmailOnly(BaseModel):
    email: str


@app.post("/api/auth/forgot")
def forgot_password(body: EmailOnly) -> dict:
    """Begin a password reset.

    The reply is deliberately identical whether or not the address is known: a
    different answer turns this into a way to discover who has an account.

    There is no mail server configured, so the link is written to the server log
    for an operator to pass on, and an administrator can generate one directly
    from the Admin screen. Wiring SMTP in here is a small change once a mailbox
    is chosen; handing the token to an anonymous caller never would be.
    """
    issued = accounts.begin_reset(body.email)
    if issued:
        token, user = issued
        link = f"{app_url()}/#/reset?token={token}"
        log.warning("Password reset requested for %s. Link: %s", user["email"], link)
        accounts.record(user, "password_reset_requested")
    return {
        "sent": True,
        "detail": "If that address has an account, a reset link has been issued. "
                  "It expires in an hour.",
    }


class ResetBody(BaseModel):
    token: str
    password: str


@app.post("/api/auth/reset")
def reset_password(body: ResetBody) -> dict:
    try:
        user = accounts.complete_reset(body.token, body.password)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    accounts.record(user, "password_reset_completed")
    return {"reset": True, "email": user["email"]}


# --------------------------------------------------------------------------- #
# The signed-in person
# --------------------------------------------------------------------------- #

@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)) -> dict:
    return user


class Profile(BaseModel):
    name: str | None = None
    email: str | None = None


@app.patch("/api/auth/me")
def update_me(body: Profile, user: dict = Depends(require_user)) -> dict:
    if user.get("is_service"):
        raise HTTPException(400, "The service key has no profile to edit.")
    try:
        updated = accounts.update_user(user["id"], name=body.name, email=body.email)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    accounts.record(updated, "profile_updated")
    return updated


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request,
                    user: dict = Depends(require_user)) -> dict:
    if user.get("is_service"):
        raise HTTPException(400, "The service key has no password.")
    try:
        accounts.authenticate(user["email"], body.current_password)
        accounts.set_password(user["id"], body.new_password)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc

    accounts.record(user, "password_changed")
    # set_password ends every session, including this one. Issue a fresh token
    # so the person who just changed their password is not thrown out by it.
    token = accounts.open_session(user["id"], request.headers.get("User-Agent"))
    return {"changed": True, "token": token}


@app.get("/api/auth/sessions")
def my_sessions(request: Request, user: dict = Depends(require_user)) -> list[dict]:
    """Where this account is signed in (gap S7).

    Without this, a session on a device you no longer have could only be ended
    by changing your password, which ends every session including the one you
    are using.
    """
    if user.get("is_service"):
        return []
    return accounts.sessions_for(user["id"], auth.bearer_token(request))


@app.delete("/api/auth/sessions/{session_id}")
def revoke_my_session(session_id: str, user: dict = Depends(require_user)) -> dict:
    if user.get("is_service"):
        raise HTTPException(400, "The service key has no sessions.")
    if not accounts.revoke_session(user["id"], session_id):
        # Deliberately the same answer whether the session belongs to someone
        # else or does not exist: neither is this caller's business.
        raise HTTPException(404, "No such session.")
    accounts.record(user, "session_revoked", {"session": session_id})
    return {"revoked": session_id}


@app.post("/api/auth/logout")
def logout(request: Request, user: dict = Depends(require_user)) -> dict:
    token = auth.bearer_token(request)
    if token:
        accounts.close_session(token)
    accounts.record(user, "logout")
    return {"logged_out": True}


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #

@app.get("/api/info")
def info(user: dict = Depends(require_user)) -> dict:
    documents = store.all_documents()
    counts = {status.value: 0 for status in DocStatus}
    for doc in documents:
        counts[doc.get("status", DocStatus.NEW.value)] = counts.get(doc.get("status"), 0) + 1

    queued = {d["period"] for d in documents if d.get("period")}
    known = sorted(set(workbook.available_periods()) | queued, key=periods.sort_key, reverse=True)

    # Holding a credential is not the same as the model having answered. An
    # expired key, an empty balance or an unreachable network all fall back to
    # the offline reader, and reporting the *configured* reader would tell
    # someone their invoices were read by a model that never saw them.
    #
    # Read from the runtime record rather than off the newest document: a note
    # on a document describes the moment it was read, which may be days ago and
    # under a different key. Quoting it in the present tense produced a screen
    # that said "your invoices may go to Gemini" and "no Gemini key configured"
    # at the same time. This returns nothing once the configuration has moved on.
    last_read = runtime.last_read(extraction_provider(), has_credentials())

    return {
        **workbook.workbook_info(),
        "reader": extraction_provider() if has_credentials() else "heuristic",
        "provider": extraction_provider(),
        "provider_tier": gemini_tier() if extraction_provider() == "gemini" else None,
        "training_risk": training_risk(),
        "reader_effective": last_read.get("reader") if last_read else None,
        "reader_note": last_read.get("note") if last_read else None,
        "last_read_at": last_read.get("at") if last_read else None,
        "model": extraction_model() if has_credentials() else None,
        "credential_source": credential_source(),
        "approval_mode": approval_mode(),
        "counts": counts,
        "periods": known,
        "max_upload_mb": max_upload_bytes() // (1024 * 1024),
        "settings": appsettings.all_settings(),
    }


@app.get("/api/dashboard")
def dashboard(user: dict = Depends(require_user)) -> dict:
    """Everything the landing screen shows, in one call."""
    documents = store.all_documents()
    counts = store.counts_by_status()

    processed = counts.get(DocStatus.POSTED.value, 0)
    failed = counts.get(DocStatus.FAILED.value, 0)
    waiting = counts.get(DocStatus.NEEDS_REVIEW.value, 0)
    ready = counts.get(DocStatus.READY.value, 0)
    in_flight = counts.get(DocStatus.NEW.value, 0)

    by_source = {}
    for doc in documents:
        by_source[doc.get("source", "upload")] = by_source.get(doc.get("source", "upload"), 0) + 1

    value = sum(_money(d) for d in documents if d.get("status") == DocStatus.POSTED.value)

    return {
        "totals": {
            "all": len(documents),
            "processed": processed,
            "failed": failed,
            "needs_review": waiting,
            "ready": ready,
            "reading": in_flight,
            "value_posted": str(value),
        },
        # Month on month, so a figure can say whether it is going the right way.
        "trend": _month_on_month(documents),
        # One point per day, for the shape of the month rather than its total.
        "arrivals": _arrivals_by_day(documents),
        "by_source": by_source,
        "top_parties": _top_parties(documents),
        # Invoice events only. The Audit screen still shows everything.
        "activity": accounts.activity(8, accounts.DOCUMENT_ACTIONS),
        "recent": [_summarise(doc) for doc in documents[:6]],
        "periods": sorted({d["period"] for d in documents if d.get("period")},
                          key=periods.sort_key, reverse=True),
    }


def _money(doc: dict) -> Decimal:
    """A document's invoice total, or zero if it never got one."""
    raw = (doc.get("treatment") or {}).get("invoice_total")
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _received_day(doc: dict) -> str | None:
    stamp = doc.get("received_at") or ""
    return stamp[:10] if len(stamp) >= 10 else None


def _month_on_month(documents: list[dict]) -> dict:
    """The last 30 days against the 30 before them, for each headline figure.

    Rolling windows rather than calendar months, deliberately. Compared by
    calendar month, every figure on this dashboard read "-100%" on the first of
    September - arithmetically true, since the month was hours old, and useless
    as a signal. A rolling window has the same length on every day of the year,
    so the comparison means the same thing whenever somebody looks at it.

    A percentage is omitted rather than invented when the earlier window was
    empty: "+100%" against a base of zero says nothing.
    """
    today = datetime.now(UTC).date()
    recent_from = (today - timedelta(days=29)).isoformat()
    prior_from = (today - timedelta(days=59)).isoformat()

    def tally(start: str, end: str | None) -> dict:
        rows = [
            d for d in documents
            if (day := _received_day(d)) and day >= start and (end is None or day < end)
        ]
        return {
            "all": len(rows),
            "processed": len([d for d in rows if d.get("status") == DocStatus.POSTED.value]),
            "needs_review": len([d for d in rows
                                 if d.get("status") == DocStatus.NEEDS_REVIEW.value]),
            "value_posted": sum(
                (_money(d) for d in rows if d.get("status") == DocStatus.POSTED.value),
                Decimal("0"),
            ),
        }

    now, before = tally(recent_from, None), tally(prior_from, recent_from)
    change: dict[str, float | None] = {}
    for key in now:
        was, is_now = float(before[key]), float(now[key])
        change[key] = round((is_now - was) / was * 100, 1) if was else None

    return {
        "window_days": 30,
        "recent": {k: str(v) for k, v in now.items()},
        "previous": {k: str(v) for k, v in before.items()},
        "change_percent": change,
    }


def _arrivals_by_day(documents: list[dict], days: int = 30) -> list[dict]:
    """Documents received per day, oldest first, with empty days kept.

    Dropping the empty days would compress the gaps and make a quiet week look
    like a busy one.
    """
    today = datetime.now(UTC).date()
    counted: dict[str, int] = {}
    for doc in documents:
        day = _received_day(doc)
        if day:
            counted[day] = counted.get(day, 0) + 1
    return [
        {"date": (day := (today - timedelta(days=offset)).isoformat()),
         "count": counted.get(day, 0)}
        for offset in range(days - 1, -1, -1)
    ]


def _top_parties(documents: list[dict], limit: int = 5) -> list[dict]:
    """Who the largest posted value sits with."""
    totals: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for doc in documents:
        if doc.get("status") != DocStatus.POSTED.value:
            continue
        name = (doc.get("treatment") or {}).get("counterparty_name")
        if not name:
            continue
        totals[name] = totals.get(name, Decimal("0")) + _money(doc)
        counts[name] = counts.get(name, 0) + 1

    ranked = sorted(totals.items(), key=lambda pair: pair[1], reverse=True)[:limit]
    return [{"name": name, "value": str(amount), "invoices": counts[name]}
            for name, amount in ranked]


def _summarise(doc: dict) -> dict:
    """The handful of fields a list row needs, rather than the whole document."""
    extracted = doc.get("extracted") or {}
    treatment = doc.get("treatment") or {}
    return {
        "id": doc["id"],
        "filename": doc["filename"],
        "invoice_number": extracted.get("invoice_number"),
        "invoice_date": extracted.get("invoice_date"),
        "party": treatment.get("counterparty_name"),
        "document_type": treatment.get("document_type"),
        "taxable_value": treatment.get("taxable_value"),
        "invoice_total": treatment.get("invoice_total"),
        "status": doc.get("status"),
        "stage": doc.get("stage"),
        "period": doc.get("period"),
        "source": doc.get("source"),
        "received_at": doc.get("received_at"),
        "sheet": doc.get("sheet"),
        "row": doc.get("row"),
        "failure_reason": doc.get("failure_reason"),
        "posted_by": doc.get("posted_by"),
        "issue_count": len([i for i in (doc.get("issues") or []) if i.get("severity") == "error"]),
    }


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

@app.get("/api/documents")
def list_documents(
    q: str | None = None,
    status: str | None = None,
    period: str | None = None,
    source: str | None = None,
    limit: int = 500,
    view: str = "full",
    user: dict = Depends(require_user),
) -> dict:
    """The invoice list, searchable and filterable.

    Search covers what a person would actually type looking for one invoice: its
    number, the other party, and the file it arrived in.

    Two shapes, because two screens need different things and quietly serving
    one to the other is how a list screen ends up with no data in it:

    - `view=full` (default) returns whole documents. The Queue and Review
      screens read the extraction, the tax treatment and the issue list.
    - `view=summary` returns one flat row per document - number, party, amount,
      status - which is all a history table renders, at a fraction of the size.
    """
    documents = store.all_documents()

    if status:
        wanted = {s.strip() for s in status.split(",") if s.strip()}
        documents = [d for d in documents if d.get("status") in wanted]
    if period:
        documents = [d for d in documents if d.get("period") == period]
    if source:
        documents = [d for d in documents if d.get("source") == source]
    if q:
        needle = q.strip().casefold()
        def matches(doc: dict) -> bool:
            extracted = doc.get("extracted") or {}
            treatment = doc.get("treatment") or {}
            haystack = [
                doc.get("filename"), doc.get("source_document"),
                extracted.get("invoice_number"), extracted.get("supplier_name"),
                extracted.get("recipient_name"), treatment.get("counterparty_name"),
                treatment.get("counterparty_gstin"),
            ]
            return any(needle in str(value).casefold() for value in haystack if value)
        documents = [d for d in documents if matches(d)]

    total = len(documents)
    page = documents[:max(1, min(limit, 1000))]
    return {
        "total": total,
        "view": view,
        "documents": [_summarise(doc) for doc in page] if view == "summary" else page,
    }


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, user: dict = Depends(require_user)) -> dict:
    doc = store.get(doc_id)
    if doc is None:
        raise HTTPException(404, "No such document.")
    return doc


@app.get("/api/documents/{doc_id}/progress")
def document_progress(doc_id: str, user: dict = Depends(require_user)) -> dict:
    """The pipeline's own progress, for the Processing screen."""
    doc = store.get(doc_id)
    if doc is None:
        raise HTTPException(404, "No such document.")

    stage = doc.get("stage") or pipeline.Stage.UPLOADED
    order = [key for key, _ in pipeline.Stage.SEQUENCE]
    failed = stage == pipeline.Stage.FAILED
    reached = -1 if failed else (order.index(stage) if stage in order else 0)

    steps = [
        {
            "key": key,
            "label": label,
            "state": "failed" if failed and index == 0 else
                     "done" if index <= reached else
                     "active" if index == reached + 1 and not failed else "waiting",
        }
        for index, (key, label) in enumerate(pipeline.Stage.SEQUENCE)
    ]
    percent = 0 if failed else round(((reached + 1) / len(order)) * 100)

    return {
        "id": doc_id,
        "stage": stage,
        "status": doc.get("status"),
        "percent": percent,
        "steps": steps,
        "failure_reason": doc.get("failure_reason"),
        "error": doc.get("error"),
        "filename": doc.get("filename"),
    }


@app.post("/api/documents")
async def upload_documents(
    request: Request,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    allow_duplicates: bool = False,
    user: dict = Depends(require_user),
) -> dict:
    """Capture one or more invoices arriving by upload.

    This answers as soon as the documents exist, before any of them has been
    read. Reading costs a model call per invoice and a print run carries
    eighteen, so doing it inline meant a browser holding a spinner for minutes
    and a timeout throwing away the response to work that had in fact happened.
    """
    limit = max_upload_bytes()
    as_mb = f"{limit // (1024 * 1024)} MB"

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"That upload is larger than the {as_mb} limit.")

    staged, errors, duplicates = [], [], []
    for upload in files:
        name = upload.filename or "document.pdf"
        data = await upload.read(limit + 1)
        if len(data) > limit:
            errors.append(f"{name}: larger than the {as_mb} limit.")
            continue
        try:
            staged.extend(pipeline.stage(data, name, "upload",
                                         allow_duplicate=allow_duplicates))
        except pipeline.DuplicateUpload as exc:
            # Not an error. The file is fine; the system simply already has it.
            duplicates.append(str(exc))
        except ValueError as exc:
            errors.append(str(exc))

    if not staged and errors:
        # Several files may have failed for different reasons; the messages are
        # already collected, and there is no single exception still in scope to
        # chain to - Python unbinds the name at the end of its except block.
        raise HTTPException(400, "; ".join(errors)) from None

    if staged:
        accounts.record(user, "uploaded", {"count": len(staged),
                                           "files": [d["filename"] for d in staged][:20]})
    background.add_task(pipeline.process_many, [doc["id"] for doc in staged])
    return {
        "captured": [_summarise(d) for d in staged],
        "errors": errors,
        "duplicates": duplicates,
        "reading": len(staged),
    }


@app.post("/api/documents/{doc_id}/reprocess")
def reprocess(doc_id: str, background: BackgroundTasks,
              user: dict = Depends(require_user)) -> dict:
    if store.get(doc_id) is None:
        raise HTTPException(404, "No such document.") from None
    store.update(doc_id, {"stage": pipeline.Stage.UPLOADED, "status": DocStatus.NEW.value,
                          "error": None, "failure_reason": None},
                 event="reread_requested", actor=user)
    accounts.record(user, "reprocess", {"document": doc_id})
    background.add_task(pipeline.process_many, [doc_id])
    return {"reading": True, "id": doc_id}


@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: str, user: dict = Depends(require_user)) -> dict:
    doc = store.get(doc_id)
    if doc is None:
        raise HTTPException(404, "No such document.")
    if doc.get("status") == DocStatus.POSTED.value:
        raise HTTPException(409, "Un-post this document before removing it.")
    store.delete(doc_id)
    accounts.record(user, "document_deleted", {"document": doc_id,
                                               "filename": doc.get("filename")})
    return {"removed": doc_id}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str, user: dict = Depends(require_user)):
    """Serve the original document for the review pane."""
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
# Review
# --------------------------------------------------------------------------- #

class Revision(BaseModel):
    """A reviewer's corrections. Only extraction fields are accepted - the tax
    treatment is always re-derived, never submitted from the browser."""

    model_config = {"extra": "allow"}


@app.patch("/api/documents/{doc_id}")
def revise_document(doc_id: str, revision: Revision,
                    user: dict = Depends(require_user)) -> dict:
    try:
        return pipeline.revise(doc_id, revision.model_dump(), actor=user)
    except KeyError:
        raise HTTPException(404, "No such document.") from None


@app.post("/api/documents/{doc_id}/confirm")
def confirm_document(doc_id: str, override: bool = False,
                     user: dict = Depends(require_user)) -> dict:
    try:
        posted = pipeline.confirm(doc_id, override=override, actor=user)
    except KeyError:
        raise HTTPException(404, "No such document.") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    accounts.record(user, "posted_with_override" if override else "posted", {
        "document": doc_id, "sheet": posted.get("sheet"), "row": posted.get("row"),
        "period": posted.get("period"),
    })
    return posted


@app.post("/api/documents/{doc_id}/unpost")
def unpost_document(doc_id: str, user: dict = Depends(require_user)) -> dict:
    try:
        result = pipeline.unpost(doc_id, actor=user)
    except KeyError:
        raise HTTPException(404, "No such document.") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    accounts.record(user, "unposted", {"document": doc_id})
    return result


# --------------------------------------------------------------------------- #
# Registers and tax position
# --------------------------------------------------------------------------- #

@app.get("/api/registers/{register}")
def register(register: str, period: str | None = None,
             user: dict = Depends(require_user)) -> dict:
    try:
        doc_type = DocumentType(register)
    except ValueError:
        raise HTTPException(404, f"Unknown register {register!r}.") from None
    period = _resolve_period(period)
    return {
        "register": doc_type.value,
        "sheet": doc_type.sheet,
        "period": period,
        "columns": workbook.register_columns(doc_type),
        "rows": workbook.read_register(doc_type, period),
    }


@app.get("/api/tax-payable")
def tax_payable(period: str | None = None, user: dict = Depends(require_user)) -> dict:
    period = _resolve_period(period)
    summary = workbook.tax_payable_summary(period).model_dump()
    summary["opening_credit_unset"] = workbook.opening_credit_is_unset(period)
    return summary


@app.get("/api/workbook/download")
def download_workbook(period: str | None = None, user: dict = Depends(require_user)):
    period = _resolve_period(period)
    workbook.ensure_working_copy(period)
    accounts.record(user, "workbook_downloaded", {"period": period})
    return FileResponse(
        workbook.workbook_path(period),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"Ira Innovations GST {period}.xlsx",
    )


@app.get("/api/workbook/download-all")
def download_all_workbooks(user: dict = Depends(require_user)):
    """Every period, as a zip of one workbook per period.

    Not one merged workbook: each period's file is a return in its own right,
    with its own totals and its own tax position, and a merged one would
    correspond to no return that can actually be filed.
    """
    available = workbook.available_periods()
    if not available:
        raise HTTPException(404, "There are no workbooks yet.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for period in available:
            workbook.ensure_working_copy(period)
            bundle.write(workbook.workbook_path(period),
                         arcname=f"Ira Innovations GST {period}.xlsx")
    buffer.seek(0)

    accounts.record(user, "workbook_downloaded", {"period": "all",
                                                  "periods": available})
    stamp = datetime.now().strftime("%Y-%m-%d")
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="Ira Innovations GST workbooks {stamp}.zip"'},
    )


@app.post("/api/workbook/reset")
def reset_workbook(period: str | None = None, user: dict = Depends(require_admin)) -> dict:
    """Discard posted rows. One period, or everything if none is named.

    Administrators only: this throws away filed rows.
    """
    workbook.reset_working_copy(period)
    if period is None:
        store.clear()
    accounts.record(user, "workbook_reset", {"period": period or "all"})
    return {"reset": period or "all"}


def _resolve_period(period: str | None) -> str:
    """Validate a requested period, defaulting to the most recent one held."""
    if period:
        if not periods.is_valid(period):
            raise HTTPException(400, f"{period!r} is not a return period, e.g. 'Jul-26'.")
        return period
    known = workbook.available_periods()
    return known[0] if known else workbook.master_period()


# --------------------------------------------------------------------------- #
# Email intake
# --------------------------------------------------------------------------- #

@app.get("/api/suppliers")
def suppliers(user: dict = Depends(require_user)) -> list[dict]:
    """Every party filed for, and what is usual for them.

    Built from posted documents only, so it reflects what a person confirmed
    rather than what a reader guessed.
    """
    return history.summary()


@app.get("/api/settings")
def get_settings(user: dict = Depends(require_user)) -> dict:
    return {"values": appsettings.all_settings(), "options": appsettings.options()}


class SettingsBody(BaseModel):
    model_config = {"extra": "allow"}


@app.put("/api/settings")
def put_settings(body: SettingsBody, user: dict = Depends(require_admin)) -> dict:
    try:
        values = appsettings.update(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    accounts.record(user, "settings_updated", body.model_dump())
    return {"values": values, "options": appsettings.options()}


# --------------------------------------------------------------------------- #
# Administration
# --------------------------------------------------------------------------- #

@app.get("/api/admin/users")
def admin_users(user: dict = Depends(require_admin)) -> list[dict]:
    return accounts.list_users()


class NewUser(BaseModel):
    email: str
    name: str = ""
    password: str
    role: str = "user"


@app.post("/api/admin/users")
def admin_create_user(body: NewUser, user: dict = Depends(require_admin)) -> dict:
    try:
        created = accounts.create_user(body.email, body.name, body.password, body.role)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    accounts.record(user, "user_created", {"email": created["email"], "role": created["role"]})
    return created


class UserPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    role: str | None = None
    is_active: bool | None = None


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, body: UserPatch,
                      user: dict = Depends(require_admin)) -> dict:
    try:
        updated = accounts.update_user(
            user_id, name=body.name, email=body.email,
            role=body.role, is_active=body.is_active,
        )
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    accounts.record(user, "user_updated", {"user": user_id, **body.model_dump(exclude_none=True)})
    return updated


@app.get("/api/admin/pending")
def admin_pending(user: dict = Depends(require_admin)) -> list[dict]:
    """Accounts that asked to join and are waiting on someone."""
    return accounts.pending_users()


@app.post("/api/admin/users/{user_id}/approve")
def admin_approve_user(user_id: str, role: str = "user",
                       user: dict = Depends(require_admin)) -> dict:
    """Let a self-registered account in, optionally as an administrator."""
    try:
        approved = accounts.approve_user(user_id)
        if role != approved["role"]:
            approved = accounts.update_user(user_id, role=role)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    accounts.record(user, "user_approved", {"email": approved["email"], "role": approved["role"]})
    return approved


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, user: dict = Depends(require_admin)) -> dict:
    if user_id == user.get("id"):
        raise HTTPException(400, "You cannot delete the account you are signed in with.")
    try:
        accounts.delete_user(user_id)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    accounts.record(user, "user_deleted", {"user": user_id})
    return {"deleted": user_id}


@app.post("/api/admin/users/{user_id}/reset-link")
def admin_reset_link(user_id: str, user: dict = Depends(require_admin)) -> dict:
    """Generate a reset link for someone who cannot receive email.

    Safe to return here because the caller is an authenticated administrator -
    unlike the anonymous forgot-password route, which must never hand a token
    back to whoever asked for it.
    """
    target = accounts.get_user(user_id)
    if target is None:
        raise HTTPException(404, "No such user.")
    issued = accounts.begin_reset(target["email"])
    if not issued:
        raise HTTPException(400, "That account cannot be reset - it may be disabled.")
    token, _ = issued
    accounts.record(user, "reset_link_issued", {"user": target["email"]})
    return {
        "email": target["email"],
        "link": f"{app_url()}/#/reset?token={token}",
        "expires_in_minutes": accounts.RESET_MINUTES,
    }


@app.get("/api/admin/activity")
def admin_activity(limit: int = 100, user: dict = Depends(require_admin)) -> list[dict]:
    return accounts.activity(limit)


@app.get("/api/admin/stats")
def admin_stats(user: dict = Depends(require_admin)) -> dict:
    documents = store.all_documents()
    counts = store.counts_by_status()
    users = accounts.list_users()

    failures = [_summarise(d) for d in documents if d.get("status") == DocStatus.FAILED.value]
    overrides = [_summarise(d) for d in documents if d.get("posted_override")]

    return {
        "users": {
            "total": len(users),
            "active": len([u for u in users if u["is_active"] and u["approved"]]),
            "admins": len([u for u in users if u["role"] == "admin"]),
            "awaiting_approval": len([u for u in users if u["awaiting_approval"]]),
        },
        "documents": {
            "total": len(documents),
            **{status.value: counts.get(status.value, 0) for status in DocStatus},
        },
        "failed_jobs": failures[:25],
        "overrides": overrides[:25],
        "reader": extraction_provider() if has_credentials() else "heuristic",
        "provider": extraction_provider(),
        "training_risk": training_risk(),
        "periods": workbook.available_periods(),
        "data_dir": str(DATA_DIR),
    }
