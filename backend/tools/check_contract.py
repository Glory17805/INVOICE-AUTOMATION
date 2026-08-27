"""Check the contract between the page and the API, against a running backend.

Why this exists: a bug reached a user that neither half could have caught alone.
The API started returning `{total, documents}` for the history screen, and the
page kept assigning that envelope straight to the variable it then iterated -
"state.documents is not iterable" on the first screen after sign-in. The backend
suite was green throughout, because the backend was right. What broke was the
agreement between the two.

So this replays the browser's own call sequence, in order, and asserts on the
fields the page actually reads, screen by screen. It is not a unit test and does
not belong in pytest: it needs a running server, and it is checking an
integration rather than a function.

    # against a scratch copy, never the live data directory
    $env:GST_DATA_DIR = "D:\\...\\backend\\data-check"
    python -m uvicorn app.main:app --port 8010 --workers 1

    python tools/check_contract.py --base http://127.0.0.1:8010

It creates an account, approves it directly through the accounts module (there
is no other way in on a fresh database), and signs in with it.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

EMAIL = "contract-check@ira.test"
PASSWORD = "a-decent-long-password"

failures: list[str] = []
token: str | None = None
base = "http://127.0.0.1:8010"


def call(method: str, path: str, body=None, expect: int = 200):
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    except OSError as exc:
        failures.append(f"{method} {path} - cannot reach {base}: {exc}")
        return None

    if status != expect:
        failures.append(f"{method} {path} -> {status}, expected {expect}: {payload[:200]!r}")
        return None
    return json.loads(payload) if payload else None


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"   {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(f"{label} {detail}".strip())


def has(obj, *keys) -> bool:
    return isinstance(obj, dict) and all(key in obj for key in keys)


def ensure_account() -> None:
    """A usable admin on whatever database the server was pointed at."""
    global token
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import accounts

    person = accounts.find_by_email(EMAIL)
    if person is None:
        accounts.create_user(EMAIL, "Contract check", PASSWORD, role="admin")
        person = accounts.find_by_email(EMAIL)
    if person["awaiting_approval"] or not person["is_active"] or person["role"] != "admin":
        accounts.approve_user(person["id"])
        accounts.update_user(person["id"], role="admin", is_active=True)


def main() -> int:
    global token, base
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=base, help="backend base URL")
    base = parser.parse_args().base.rstrip("/")

    print("\n-- signed out --")
    boot = call("GET", "/api/bootstrap")
    check("bootstrap carries needs_setup and signup_mode",
          has(boot, "needs_setup", "signup_mode"), str(boot))
    call("GET", "/api/info", expect=401)
    check("a protected route refuses an anonymous caller", True)

    ensure_account()

    print("\n-- sign in --")
    signed = call("POST", "/api/auth/login", {"email": EMAIL, "password": PASSWORD})
    if not signed or "token" not in signed:
        print("\ncould not sign in; stopping")
        return 1
    token = signed["token"]
    check("login returns a token and a user", has(signed, "token", "user"))
    check("the user carries name, email and role", has(signed["user"], "name", "email", "role"))
    check("/api/auth/me resolves the session",
          has(call("GET", "/api/auth/me"), "id", "email", "role", "approved"))

    print("\n-- enterApp: loadInfo, then loadDocuments --")
    info = call("GET", "/api/info")
    for key in ("company", "gstin", "periods", "counts", "max_upload_mb", "reader"):
        check(f"info.{key}", info is not None and key in info)

    page = call("GET", "/api/documents?view=full")
    check("documents come back as an envelope", has(page, "total", "documents"))
    check("...whose .documents is a list", isinstance((page or {}).get("documents"), list))

    docs = (page or {}).get("documents") or []
    if docs:
        first = docs[0]
        for key in ("id", "filename", "status"):
            check(f"a full document has .{key}", key in first)
        # The regression that started this file: summaries drop these, and the
        # Queue and Review screens are built on them.
        for key in ("extracted", "treatment", "issues"):
            check(f"a full document keeps .{key}", key in first,
                  "a summary would have dropped it")
    else:
        print("  (no documents on this database; document shapes not checked)")

    print("\n-- Dashboard --")
    dash = call("GET", "/api/dashboard")
    check("dashboard envelope", has(dash, "totals", "recent", "by_source"))
    if dash:
        for key in ("all", "processed", "failed", "needs_review", "ready", "reading"):
            check(f"totals.{key}", key in dash["totals"])
        if dash["recent"]:
            for key in ("id", "filename", "invoice_number", "party", "status", "invoice_total"):
                check(f"a recent row has .{key}", key in dash["recent"][0])

    print("\n-- History --")
    hist = call("GET", "/api/documents?view=summary&limit=5")
    check("history envelope", has(hist, "total", "documents"))
    if hist and hist["documents"]:
        for key in ("id", "filename", "invoice_number", "party", "invoice_date",
                    "period", "source", "invoice_total", "status"):
            check(f"a history row has .{key}", key in hist["documents"][0])
    call("GET", "/api/documents?view=summary&q=IRA&status=posted")
    check("search and filter combine", True)

    print("\n-- Registers and tax position --")
    for register in ("sales", "purchase", "credit_note", "rcm"):
        check(f"register {register}",
              has(call("GET", f"/api/registers/{register}"),
                  "register", "sheet", "period", "columns", "rows"))
    tax = call("GET", "/api/tax-payable")
    for key in ("itc_available", "output_tax", "net_payable", "return_period",
                "opening_credit_unset"):
        check(f"tax.{key}", tax is not None and key in tax)

    print("\n-- Email intake and settings --")
    check("email status", has(call("GET", "/api/email/status"),
                              "mode", "connected", "folder", "waiting",
                              "mailbox_connector", "rules"))
    settings = call("GET", "/api/settings")
    check("settings envelope", has(settings, "values", "options"))
    modes = (settings or {}).get("options", {}).get("signup_mode")
    check("signup_mode options are {value,label}",
          isinstance(modes, list) and bool(modes) and has(modes[0], "value", "label"))

    print("\n-- Admin --")
    users = call("GET", "/api/admin/users")
    check("users is a list", isinstance(users, list))
    if users:
        check("a user row distinguishes approved from active",
              has(users[0], "approved", "awaiting_approval", "is_active", "role"))
    stats = call("GET", "/api/admin/stats")
    check("stats envelope", has(stats, "users", "documents", "failed_jobs", "overrides",
                                "reader", "periods", "data_dir"))
    check("stats.users.awaiting_approval", stats is not None
          and "awaiting_approval" in stats["users"])
    activity = call("GET", "/api/admin/activity?limit=5")
    check("activity is a list", isinstance(activity, list))
    if activity:
        check("an activity row has when/who/what",
              has(activity[0], "at", "user_email", "action", "detail"))
    check("pending is a list", isinstance(call("GET", "/api/admin/pending"), list))

    print("\n-- Processing --")
    if docs:
        progress = call("GET", f"/api/documents/{docs[0]['id']}/progress")
        check("progress envelope",
              has(progress, "id", "stage", "status", "percent", "steps", "filename"))
        if progress and progress["steps"]:
            check("a step has key, label and state",
                  has(progress["steps"][0], "key", "label", "state"))

    print("\n-- sign out --")
    call("POST", "/api/auth/logout")
    call("GET", "/api/info", expect=401)
    check("the session stops working", True)

    print("\n" + "=" * 62)
    if failures:
        print(f"{len(failures)} problem(s):")
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print("every screen's contract holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
