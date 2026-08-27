"""Provider selection: which reader runs, and what happens when it cannot.

The invariant that matters is that the reader REPORTED is the reader that
actually ran. A provider that is configured but refuses - no credit, exhausted
quota, a rejected key - must fall through to the offline reader and say why,
because "every invoice is being read offline" is the kind of thing that is
expensive to discover late.
"""


import pytest

from app import config, pipeline
from app.extract import gemini, llm

from .test_gst import invoice


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Neither provider configured unless a test says so."""
    for name in ("GST_EXTRACTION_PROVIDER", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GST_GEMINI_MODEL",
                 "GST_EXTRACTION_MODEL"):
        monkeypatch.setenv(name, "")
    # .env would otherwise supply the real keys underneath the env vars.
    monkeypatch.setattr(config, "_dotenv", dict)
    monkeypatch.setattr(config, "anthropic_credentials", lambda: False)


# --------------------------------------------------------------------------- #
# Choosing a provider
# --------------------------------------------------------------------------- #

def test_a_gemini_key_selects_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert config.extraction_provider() == "gemini"
    assert config.has_credentials()
    assert config.extraction_model() == "gemini-3.6-flash"


def test_google_api_key_is_accepted_as_well(monkeypatch):
    """Google AI Studio hands out the key under either name."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert config.extraction_provider() == "gemini"


def test_anthropic_credentials_select_claude(monkeypatch):
    monkeypatch.setattr(config, "anthropic_credentials", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert config.extraction_provider() == "claude"
    assert config.extraction_model() == "claude-opus-5"


def test_an_explicit_provider_overrides_which_keys_are_present(monkeypatch):
    """Pinning the provider stops a stray key from redirecting the bill."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GST_EXTRACTION_PROVIDER", "claude")
    assert config.extraction_provider() == "claude"
    # ...and with no Claude credential it has none, rather than borrowing Gemini's.
    assert not config.has_credentials()


def test_the_gemini_model_is_overridable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GST_GEMINI_MODEL", "gemini-2.5-pro")
    assert config.extraction_model() == "gemini-2.5-pro"


def test_a_claude_model_name_does_not_leak_into_gemini(monkeypatch):
    """A GST_EXTRACTION_MODEL left over from the Claude setup must not be sent
    to Gemini, which would fail with a confusing model-not-found."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GST_EXTRACTION_MODEL", "claude-opus-5")
    assert config.extraction_model() == "gemini-3.6-flash"


# --------------------------------------------------------------------------- #
# Dispatch, and falling back
# --------------------------------------------------------------------------- #

@pytest.fixture
def provider_enabled(monkeypatch):
    """Undo conftest's offline guard for tests that mock the reader instead.

    These never touch the network - they replace `extract` - so the blanket
    guard would only stop them exercising the dispatch they exist to test.
    """
    monkeypatch.setattr(pipeline, "has_credentials", config.has_credentials)


def _fake_read(monkeypatch, module, result):
    def fake(path):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(module, "extract", fake)


def test_the_reader_reported_is_the_reader_that_ran(monkeypatch, tmp_path, provider_enabled):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    doc = invoice(invoice_number="X-1", invoice_date="31-May-2026")
    _fake_read(monkeypatch, gemini, doc)

    extracted, reader, note = pipeline._read_document(tmp_path / "any.pdf")
    assert reader == "gemini"
    assert note is None
    assert extracted.invoice_number == "X-1"


def test_a_refusing_provider_falls_back_and_says_why(monkeypatch, tmp_path, provider_enabled):
    """Exactly the Anthropic no-credit case, which reported 'Claude reader'
    while every document was in fact read offline."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _fake_read(monkeypatch, gemini, llm.ExtractionUnavailable("credit balance is too low"))
    monkeypatch.setattr(pipeline.heuristic, "extract", lambda p: invoice(invoice_number="H-1"))

    _, reader, note = pipeline._read_document(tmp_path / "any.pdf")
    assert reader == "heuristic"
    assert "credit balance is too low" in note


def test_an_unexpected_error_still_captures_the_document(monkeypatch, tmp_path, provider_enabled):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _fake_read(monkeypatch, gemini, RuntimeError("socket exploded"))
    monkeypatch.setattr(pipeline.heuristic, "extract", lambda p: invoice(invoice_number="H-1"))

    _, reader, note = pipeline._read_document(tmp_path / "any.pdf")
    assert reader == "heuristic"
    assert "socket exploded" in note


def test_no_key_at_all_names_the_provider_it_wants(monkeypatch, tmp_path):
    monkeypatch.setenv("GST_EXTRACTION_PROVIDER", "gemini")
    monkeypatch.setattr(pipeline.heuristic, "extract", lambda p: invoice())

    _, reader, note = pipeline._read_document(tmp_path / "any.pdf")
    assert reader == "heuristic"
    assert "GEMINI_API_KEY" in note

    monkeypatch.setenv("GST_EXTRACTION_PROVIDER", "claude")
    _, _, note = pipeline._read_document(tmp_path / "any.pdf")
    assert "ANTHROPIC_API_KEY" in note


# --------------------------------------------------------------------------- #
# The Gemini reader itself
# --------------------------------------------------------------------------- #

def test_gemini_refuses_without_a_key(monkeypatch, tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(llm.ExtractionUnavailable, match="GEMINI_API_KEY"):
        gemini.extract(pdf)


@pytest.mark.parametrize("raw,expected", [
    ("API key not valid. Please pass a valid API key.", "rejected"),
    ("429 RESOURCE_EXHAUSTED: quota exceeded", "quota"),
    ("403 PERMISSION_DENIED", "refused"),
    ("504 deadline exceeded", "did not respond in time"),
    ("something else entirely", "Gemini error"),
])
def test_sdk_errors_become_sentences_a_reviewer_can_act_on(raw, expected):
    assert expected in gemini._explain(Exception(raw))


def test_both_providers_share_one_prompt():
    """The instruction not to decide the tax treatment is the load-bearing part
    of the prompt; it must not drift between providers."""
    assert gemini.SYSTEM_PROMPT is llm.SYSTEM_PROMPT
    assert "Do not compute, correct, or reconcile" in gemini.SYSTEM_PROMPT


def test_an_oversized_pdf_is_refused_before_it_is_uploaded(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n" + b"0" * (31 * 1024 * 1024))
    with pytest.raises(llm.ExtractionUnavailable, match="30 MB"):
        gemini.extract(big)


# --------------------------------------------------------------------------- #
# The free-tier data-handling warning
# --------------------------------------------------------------------------- #

def test_free_tier_is_assumed_until_someone_says_otherwise(monkeypatch):
    """The tier cannot be read off the key, so the risky case is the default."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert config.gemini_tier() == "free"

    warning = config.training_risk()
    assert warning is not None
    assert "improve their models" in warning
    assert "GST_GEMINI_TIER=paid" in warning


def test_declaring_a_paid_tier_clears_the_warning(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GST_GEMINI_TIER", "paid")
    assert config.gemini_tier() == "paid"
    assert config.training_risk() is None


def test_claude_raises_no_training_warning(monkeypatch):
    monkeypatch.setattr(config, "anthropic_credentials", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert config.training_risk() is None


def test_no_warning_when_gemini_is_selected_but_has_no_key(monkeypatch):
    """Nothing is being sent anywhere, so there is nothing to warn about."""
    monkeypatch.setenv("GST_EXTRACTION_PROVIDER", "gemini")
    assert config.training_risk() is None


@pytest.mark.parametrize("declared,expected", [
    ("paid", "paid"), ("PAID", "paid"), ("billed", "paid"), ("vertex", "paid"),
    ("free", "free"), ("", "free"), ("nonsense", "free"),
])
def test_tier_declarations_resolve_conservatively(monkeypatch, declared, expected):
    monkeypatch.setenv("GST_GEMINI_TIER", declared)
    assert config.gemini_tier() == expected


def test_the_suite_cannot_reach_a_provider_by_accident():
    """conftest forces the offline reader. If this fails, the suite is making
    real API calls - slow, quota-burning, and dependent on a network."""
    assert pipeline.has_credentials() is False


def test_the_scan_message_names_the_active_providers_key(monkeypatch):
    """A message that names the wrong key sends someone to a credential that
    cannot help them. This went stale the moment a second provider existed."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert pipeline._provider_key_name() == "GEMINI_API_KEY"
    assert "Gemini" in pipeline._provider_label()

    monkeypatch.setenv("GST_EXTRACTION_PROVIDER", "claude")
    assert pipeline._provider_key_name() == "ANTHROPIC_API_KEY"
    assert "Claude" in pipeline._provider_label()
