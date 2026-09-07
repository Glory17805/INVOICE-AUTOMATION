"""Deployment configuration and the company profile the GST rules are applied from."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent   # the backend/ directory
PROJECT_ROOT = APP_ROOT.parent                       # gst-automation/


def _data_dir() -> Path:
    """Where the workbooks, the queue and the archive live.

    Overridable with GST_DATA_DIR. Read from the real environment only, not
    .env: pointing the data somewhere else is a deployment decision, and it has
    to be settable before anything reads the file that would configure it.

    This exists because the single-instance guard tells a second backend to
    "point this one at a different data directory", and an instruction the
    application cannot carry out is worse than no instruction.
    """
    configured = os.environ.get("GST_DATA_DIR", "").strip()
    if not configured:
        return APP_ROOT / "data"
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else (APP_ROOT / candidate).resolve()


DATA_DIR = _data_dir()
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


# Which reader the pipeline uses. Claude and Gemini are interchangeable at the
# `extract(path) -> ExtractedInvoice` boundary; the offline reader is the
# fallback when neither has usable credentials.
# "offline" is a first-class choice, not the absence of one. Running without a
# model is a legitimate way to use this system - nothing leaves the machine,
# and text-layer PDFs still read - so it needs to be configurable as an
# intention rather than inferred from a missing key. The difference is visible:
# a missing key is reported as degraded, offline is reported as chosen.
PROVIDERS = ("claude", "gemini", "offline")

DEFAULT_MODELS = {
    "claude": "claude-opus-5",
    # Verified against a live free-tier key: 2.5-flash is refused for new
    # users ("no longer available"), and the model list still advertises it,
    # so the list is not a safe source. 3.6-flash is what the API itself
    # points new keys at, and it honours the response schema.
    "gemini": "gemini-3.6-flash",
}


def extraction_provider() -> str:
    """Which LLM reads invoices.

    Set GST_EXTRACTION_PROVIDER to pin one. Left unset, whichever provider has
    a key wins, so adding a key is the only step needed to switch - and a key
    that is present but broken does not silently route to the other provider's
    bill.
    """
    configured = setting("GST_EXTRACTION_PROVIDER").lower()
    if configured in PROVIDERS:
        return configured
    if gemini_key():
        return "gemini"
    if anthropic_credentials():
        return "claude"
    return "gemini" if configured == "" else "claude"


def gemini_key() -> str:
    return setting("GEMINI_API_KEY") or setting("GOOGLE_API_KEY")


def gemini_tier() -> str:
    """Whether this Gemini key is on the free tier or a paid one.

    This has to be declared, because it cannot be detected: an AI Studio key is
    the same string whether or not billing is enabled on its project, and the
    API exposes no "which tier am I" endpoint. The default is "free" on
    purpose - the conservative assumption is the one where the data is at risk,
    so an operator who has not thought about it gets the warning rather than
    silence.

    It matters because Google may use free-tier inputs to improve their models,
    and the inputs here are a real company's invoices: named counterparties,
    GSTINs, amounts.
    """
    declared = setting("GST_GEMINI_TIER", "free").lower()
    return "paid" if declared in ("paid", "billed", "vertex") else "free"


def training_risk() -> str | None:
    """A one-line warning when invoice data may be used to train a model.

    Returns None when there is nothing to warn about, so callers can treat a
    value as "show this".
    """
    if extraction_provider() != "gemini" or not gemini_key():
        return None
    if gemini_tier() == "paid":
        return None
    return (
        "Gemini free tier: Google may use these invoices to improve their models. "
        "Do not use it for a real client's filings - set GST_GEMINI_TIER=paid in "
        "backend/.env once billing is enabled, or switch provider."
    )


def gemini_model() -> str:
    return setting("GST_GEMINI_MODEL", DEFAULT_MODELS["gemini"])


def extraction_model() -> str:
    """The model the active provider will use."""
    configured = setting("GST_EXTRACTION_MODEL")
    provider = extraction_provider()
    if provider == "offline":
        return "offline reader"
    if provider == "gemini":
        return setting("GST_GEMINI_MODEL") or (
            configured if configured.startswith("gemini") else DEFAULT_MODELS["gemini"]
        )
    return configured or DEFAULT_MODELS["claude"]


def anthropic_credentials() -> bool:
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


def has_credentials() -> bool:
    """Whether the active provider can authenticate at all.

    Offline needs no credential and is not missing one, so it answers True:
    the reader it wants is available. Answering False would report a
    deliberate choice as a broken configuration.
    """
    provider = extraction_provider()
    if provider == "offline":
        return True
    if provider == "gemini":
        return bool(gemini_key())
    return anthropic_credentials()


def credential_source() -> str | None:
    """Which credential the app will use, for display on the header."""
    if extraction_provider() == "offline":
        return "no credential needed"
    if extraction_provider() == "gemini":
        return "API key" if gemini_key() else None
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


def app_url() -> str:
    """Where the frontend is reachable, used to build password-reset links.

    The backend never serves the page, so it cannot infer this from a request:
    the browser's address and the API's address are different by design.
    """
    return setting("GST_APP_URL", f"http://127.0.0.1:{setting('GST_FRONTEND_PORT', '3000')}").rstrip("/")


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
