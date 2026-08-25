"""Request authentication for the API.

The API hands out the GST workbook and the original invoices behind it, so the
moment it is reachable from anything but localhost it needs a door. This is the
proportionate one for a single-tenant internal tool: a shared secret, checked in
constant time, on every route that is not a liveness probe.

It is deliberately not a login. There is one key for the whole deployment, so it
identifies *the deployment*, not the person using it. Per-user accounts belong
with per-user permissions and an audit trail of who posted which row, which is a
larger change than a header check and is recorded as future work.

Unset by default: `GST_API_KEY` empty means the API is open, which is the right
default for someone running both halves on their own machine. The startup banner
says which mode is in force so that an unauthenticated deployment is never a
silent surprise.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import api_key

HEADER = "X-API-Key"

# Reachable without a key. The liveness probe has to answer before a caller can
# be expected to hold credentials, and it discloses nothing but "the process is
# up". CORS preflights are also let through: the browser sends OPTIONS *before*
# it will attach a custom header, so rejecting them would make every
# authenticated cross-origin call fail at the preflight.
OPEN_PATHS = frozenset({"/api/health"})


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require a matching key on every /api route once one is configured."""

    async def dispatch(self, request, call_next):
        expected = api_key()

        # No key configured: the API is open by choice, not by accident.
        if not expected:
            return await call_next(request)

        path = request.url.path
        if request.method == "OPTIONS" or path in OPEN_PATHS or not path.startswith("/api/"):
            return await call_next(request)

        presented = _presented_key(request)
        if presented is None or not secrets.compare_digest(presented, expected):
            return JSONResponse(
                {"detail": f"This API requires a valid {HEADER} header."},
                status_code=401,
            )
        return await call_next(request)


def _presented_key(request) -> str | None:
    """The key the caller offered, by header or bearer token.

    Accepting `Authorization: Bearer` as well as the dedicated header costs
    nothing and means curl, Postman and any generic HTTP client work without
    special-casing. Query parameters are deliberately *not* accepted: they end
    up in browser history, proxy logs and referrer headers.
    """
    header = request.headers.get(HEADER)
    if header:
        return header.strip()

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None
