"""Deployment configuration and the company profile the GST rules are applied from."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent   # the backend/ directory
PROJECT_ROOT = APP_ROOT.parent                       # gst-automation/
DATA_DIR = APP_ROOT / "data"
INCOMING_DIR = DATA_DIR / "incoming"
ARCHIVE_DIR = DATA_DIR / "archive"
WORKBOOK_DIR = DATA_DIR / "workbook"
STORE_PATH = DATA_DIR / "store.json"


_ENV_PATH = APP_ROOT / ".env"
_env_cache: tuple[float, dict[str, str]] = (0.0, {})


def _dotenv() -> dict[str, str]:
    """Parse .env, re-reading it whenever the file changes on disk.

    Re-reading rather than loading once at import means adding an API key takes
    effect on the next request - no restart. That matters because the whole
    point of the offline fallback is that someone is sitting there without a
    key, and making them restart the server to use one is a poor trade.
    """
    global _env_cache
    try:
        stamp = _ENV_PATH.stat().st_mtime
    except OSError:
        return {}
    if stamp == _env_cache[0]:
        return _env_cache[1]

    values: dict[str, str] = {}
    for raw in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            values[key.strip()] = value
    _env_cache = (stamp, values)
    return values


def setting(name: str, default: str = "") -> str:
    """A configuration value: real environment first, then .env."""
    return os.environ.get(name, "").strip() or _dotenv().get(name, "").strip() or default


@dataclass(frozen=True)
class Company:
    """The entity the workbook belongs to.

    Every classification decision ("are we the seller or the buyer?") and every
    intra- vs inter-state call is made relative to this profile.
    """

    name: str
    gstin: str
    state_code: str
    state_name: str

    @property
    def normalised_name(self) -> str:
        return self.name.strip().casefold()


# Read off the GSTR-1 sheet header and the sample tax invoice.
IRA_INNOVATIONS = Company(
    name="Ira Innovations",
    gstin="37AAKFI3341N1Z0",
    state_code="37",
    state_name="Andhra Pradesh",
)


def source_workbook() -> Path:
    configured = setting("GST_SOURCE_WORKBOOK")
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = (APP_ROOT / candidate).resolve()
        return candidate
    return (PROJECT_ROOT.parent / "01 Ira Innovations May-26 GST Calculation.xlsx").resolve()


def approval_mode() -> str:
    """Whether every row needs a human, or only the ones validation flags.

    The blueprint leaves this open ("whether a human must approve every row
    before it's posted, or only the ones validation flags"), so it is a setting
    rather than a hard-coded policy. Either way nothing reaches the workbook
    without someone pressing Post - this only decides which queue a clean
    document lands in.
    """
    mode = setting("GST_APPROVAL_MODE", "flagged_only").lower()
    return mode if mode in {"every_row", "flagged_only"} else "flagged_only"


def extraction_model() -> str:
    return setting("GST_EXTRACTION_MODEL", "claude-opus-5")


def has_credentials() -> bool:
    """Whether the Claude SDK has any credential to authenticate with.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials: the SDK
    also accepts an auth token, and picks up a stored profile from `ant auth
    login` with no environment variable set at all. Checking only the API key
    would silently drop the app into the offline reader when it could in fact
    reach Claude.
    """
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if setting(variable):
            return True
    profile_dir = Path.home() / ".config" / "anthropic"
    return profile_dir.is_dir() and any(profile_dir.iterdir())


def credential_source() -> str | None:
    """Which credential the app will use, for display on the header."""
    if setting("ANTHROPIC_API_KEY"):
        return "API key"
    if setting("ANTHROPIC_AUTH_TOKEN"):
        return "auth token"
    profile_dir = Path.home() / ".config" / "anthropic"
    if profile_dir.is_dir() and any(profile_dir.iterdir()):
        return "stored profile"
    return None


def api_key() -> str:
    """The shared secret the API requires, or "" to run unauthenticated.

    This is a network boundary, not a user identity: it stops the API from
    being callable by anything that can merely reach the port, which is the
    exposure that matters the moment this leaves localhost. Everything it
    guards - the workbook, the original invoices - is readable by anyone who
    holds the key, so it is a single-tenant internal control and no substitute
    for real per-user authentication.
    """
    return setting("GST_API_KEY")


def max_upload_bytes() -> int:
    """Largest single upload accepted, so one file cannot exhaust the disk."""
    try:
        megabytes = float(setting("GST_MAX_UPLOAD_MB", "25"))
    except ValueError:
        megabytes = 25.0
    return int(max(megabytes, 1) * 1024 * 1024)


def lock_path() -> Path:
    """The file whose lock marks this data directory as claimed."""
    return DATA_DIR / "backend.lock"


def frontend_origins() -> list[str]:
    """Which origins the browser may call this API from.

    The frontend is a separate server on its own port, so every request it
    makes is cross-origin. Listing the origins explicitly rather than allowing
    "*" keeps the API from being callable by any page the user happens to have
    open.
    """
    configured = setting("GST_FRONTEND_ORIGINS")
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    port = setting("GST_FRONTEND_PORT", "3000")
    return [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]


def ensure_dirs() -> None:
    for path in (DATA_DIR, INCOMING_DIR, ARCHIVE_DIR, WORKBOOK_DIR):
        path.mkdir(parents=True, exist_ok=True)
