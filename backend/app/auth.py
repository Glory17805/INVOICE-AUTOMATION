"""Who is calling, and may they.

Requests carry a session token as `Authorization: Bearer <token>`. Routes state
their own requirement with a dependency - `Depends(require_user)` or
`Depends(require_admin)` - rather than a blanket middleware, because "everything
is protected except this list" is the shape that eventually leaks a route.

A deployment may also set GST_API_KEY. That is a *service* credential for
scripts and integrations, not a person: it authenticates as a synthetic
principal whose actions are recorded in the audit trail as such, so a row posted
by an automation is never mistaken for one a person approved.

Failed logins are throttled per email-and-address. A shared secret on an open
port invites guessing, and nothing else here slows that down.
"""

from __future__ import annotations

import secrets
import time
from threading import RLock

from fastapi import HTTPException, Request

from . import accounts
from .config import api_key

# A principal that is not a user row: the deployment's own service key.
SERVICE_PRINCIPAL = {
    "id": "service",
    "email": "service-key",
    "name": "Service key",
    "role": "admin",
    "is_active": True,
    "is_service": True,
}

# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #

_ATTEMPT_LIMIT = 5           # failures before a wait is imposed
_WINDOW_SECONDS = 15 * 60    # failures older than this are forgotten
_LOCKOUT_SECONDS = 60        # grows with each further failure, to a ceiling
_LOCKOUT_CEILING = 15 * 60

_attempts: dict[str, list[float]] = {}
_attempt_lock = RLock()


def _throttle_key(email: str, request: Request) -> str:
    address = request.client.host if request.client else "?"
    return f"{(email or '').strip().casefold()}|{address}"


def retry_after(email: str, request: Request) -> int:
    """Seconds the caller must wait, or 0 if they may try now."""
    key = _throttle_key(email, request)
    now = time.time()
    with _attempt_lock:
        failures = [t for t in _attempts.get(key, []) if now - t < _WINDOW_SECONDS]
        _attempts[key] = failures
    if len(failures) < _ATTEMPT_LIMIT:
        return 0
    # Each failure past the limit doubles the wait, up to the ceiling.
    over = len(failures) - _ATTEMPT_LIMIT
    wait = min(_LOCKOUT_SECONDS * (2 ** over), _LOCKOUT_CEILING)
    elapsed = now - failures[-1]
    return max(0, int(wait - elapsed))


def note_failure(email: str, request: Request) -> None:
    key = _throttle_key(email, request)
    with _attempt_lock:
        _attempts.setdefault(key, []).append(time.time())


def clear_failures(email: str, request: Request) -> None:
    with _attempt_lock:
        _attempts.pop(_throttle_key(email, request), None)


def reset_throttle() -> None:
    """Tests only."""
    with _attempt_lock:
        _attempts.clear()


# --------------------------------------------------------------------------- #
# Resolving the caller
# --------------------------------------------------------------------------- #

def bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    # Accepted for convenience from curl and the like.
    direct = request.headers.get("X-API-Key")
    return direct.strip() if direct else None


def caller(request: Request) -> dict | None:
    """The principal behind this request, or None if it is anonymous."""
    token = bearer_token(request)
    if not token:
        return None

    configured = api_key()
    if configured and secrets.compare_digest(token, configured):
        return dict(SERVICE_PRINCIPAL)

    return accounts.user_for_token(token)


def require_user(request: Request) -> dict:
    person = caller(request)
    if person is None:
        raise HTTPException(401, "Sign in to continue.")
    request.state.user = person
    return person


def require_admin(request: Request) -> dict:
    person = require_user(request)
    if person.get("role") != "admin":
        raise HTTPException(403, "This needs an administrator account.")
    return person


def optional_user(request: Request) -> dict | None:
    person = caller(request)
    if person is not None:
        request.state.user = person
    return person
