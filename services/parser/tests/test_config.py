"""Task Group 1: settings load with the PARSER_ prefix and correct defaults.

Focused scaffold coverage only — exhaustive config-permutation coverage is
deliberately skipped (per task 1.1).
"""

from app.config import Settings

# The PARSER_-prefixed knobs this suite asserts defaults for. Cleared before
# constructing Settings so a caller's ambient env cannot mask a wrong default.
_PARSER_ENV_VARS = (
    "PARSER_ESCALATION_ENGINE",
    "PARSER_MAX_PAGES",
    "PARSER_MAX_INPUT_MB",
    "PARSER_BUDGET_USD",
    "PARSER_SCAN_FASTPATH",
    "PARSER_ESCALATION_ARBITRATION",
    "PARSER_VLM_MODEL",
    "PARSER_IDLE_TIMEOUT_SECONDS",
)


def test_settings_defaults(monkeypatch):
    for var in _PARSER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    settings = Settings()

    # Textract is the default escalation engine.
    assert settings.escalation_engine == "textract"
    # Operational guardrails.
    assert settings.max_pages == 100
    assert settings.max_input_mb == 50.0
    # Spend guard disabled by default (page cap × concurrency is the bound).
    assert settings.budget_usd is None
    # Both opt-in toggles default OFF (byte-identical to baseline).
    assert settings.scan_fastpath is False
    assert settings.escalation_arbitration is False
    # Concurrency = 1 in-flight parse per pod.
    assert settings.max_concurrent_parses == 1
    # VLM model default (used only when engine == "vlm").
    assert settings.vlm_model == "au.anthropic.claude-sonnet-4-6"
    # Idle-timeout knob aligns with the BFF's 120 s idle constant.
    assert settings.idle_timeout_seconds == 120


def test_settings_read_parser_prefixed_env_and_bare_aws_region(monkeypatch):
    monkeypatch.setenv("PARSER_ESCALATION_ENGINE", "vlm")
    monkeypatch.setenv("PARSER_MAX_PAGES", "50")
    monkeypatch.setenv("PARSER_MAX_INPUT_MB", "25")
    monkeypatch.setenv("PARSER_BUDGET_USD", "5.00")
    monkeypatch.setenv("PARSER_SCAN_FASTPATH", "1")
    monkeypatch.setenv("PARSER_ESCALATION_ARBITRATION", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    settings = Settings()

    assert settings.escalation_engine == "vlm"
    assert settings.max_pages == 50
    assert settings.max_input_mb == 25.0
    assert settings.budget_usd == 5.00
    assert settings.scan_fastpath is True
    assert settings.escalation_arbitration is True
    # Standard AWS variable stays unprefixed.
    assert settings.aws_region == "us-east-1"


def test_guardrail_and_budget_env_resolve(monkeypatch):
    """Task Group 5: the compose-wired guardrail/budget knobs resolve.

    Asserts the exact env names compose sets on the parser service map onto the
    typed settings — the budget guard stays DISABLED unless explicitly set, and
    the caps / idle timeout honour their overrides.
    """
    for var in _PARSER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Default state (nothing set): budget disabled, documented caps in force.
    default = Settings()
    assert default.budget_usd is None
    assert default.max_pages == 100
    assert default.max_input_mb == 50.0
    assert default.idle_timeout_seconds == 120

    # Explicit compose-style overrides resolve onto the typed view.
    monkeypatch.setenv("PARSER_MAX_PAGES", "200")
    monkeypatch.setenv("PARSER_MAX_INPUT_MB", "100")
    monkeypatch.setenv("PARSER_BUDGET_USD", "12.50")
    monkeypatch.setenv("PARSER_IDLE_TIMEOUT_SECONDS", "180")

    overridden = Settings()
    assert overridden.max_pages == 200
    assert overridden.max_input_mb == 100.0
    assert overridden.budget_usd == 12.50
    assert overridden.idle_timeout_seconds == 180
