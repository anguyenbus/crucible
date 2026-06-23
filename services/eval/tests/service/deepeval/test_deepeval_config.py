"""Tests for DeepEval configuration."""

import os
from unittest import mock

import pytest

from app.deepeval.bedrock_provider import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_TEMPERATURE,
    get_deepeval_config,
    get_deepeval_llm,
)

# Judge-related CRUCIBLE_* / AWS_* env vars touched by these tests. We clear
# them per-test so suite ordering cannot leak provider/model/region state.
_JUDGE_ENV_VARS = (
    "CRUCIBLE_JUDGE_PROVIDER",
    "CRUCIBLE_JUDGE_MODEL",
    "CRUCIBLE_GENERATOR_MODEL",
    "DEEPEVAL_MAX_CONCURRENT",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


@pytest.fixture(autouse=True)
def _clean_judge_env(monkeypatch):
    """Start each test from a known-clean judge/region env."""
    for var in _JUDGE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


def test_deepeval_telemetry_disabled():
    """Test that DeepEval telemetry is disabled."""
    assert os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") == "YES"


def test_constants():
    """Test module constants."""
    assert DEFAULT_OPENAI_MODEL == "gpt-4o-mini"
    assert DEFAULT_TEMPERATURE == 0.0
    assert DEFAULT_MAX_CONCURRENT == 10
    # Bedrock-default: DEFAULT_BEDROCK_MODEL is now the resolved au.-profile
    # judge default (was the bare-family anthropic.claude-3-5-sonnet ID).
    assert DEFAULT_BEDROCK_MODEL == "au.anthropic.claude-opus-4-6"


def test_get_openai_api_key_missing(monkeypatch):
    """Test that missing API key raises ValueError."""
    from app.deepeval.bedrock_provider import _get_openai_api_key

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY.*must be set"):
        _get_openai_api_key()


def test_get_openai_api_key_from_env(monkeypatch):
    """Test API key from environment."""
    from app.deepeval.bedrock_provider import _get_openai_api_key

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert _get_openai_api_key() == "test-key"


# ---------------------------------------------------------------------------
# Task Group 4: judge provider/model/region wiring + global judge config
# ---------------------------------------------------------------------------


def test_get_deepeval_llm_bedrock_passes_region_no_credentials(monkeypatch):
    """Bedrock branch passes region to AmazonBedrockModel with NO credentials."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")

    fake_model = mock.MagicMock(name="AmazonBedrockModelInstance")
    fake_cls = mock.MagicMock(name="AmazonBedrockModel", return_value=fake_model)
    fake_module = mock.MagicMock()
    fake_module.AmazonBedrockModel = fake_cls

    # Patch at the lazy import site (deepeval.models) so the function picks up
    # our fake when it imports inside the bedrock branch.
    with mock.patch.dict("sys.modules", {"deepeval.models": fake_module}):
        result = get_deepeval_llm(
            provider="bedrock",
            model="au.anthropic.claude-opus-4-6",
            temperature=0.0,
        )

    assert result is fake_model
    fake_cls.assert_called_once()
    _, kwargs = fake_cls.call_args
    # Region is passed explicitly (env > YAML; no implicit us-east-1).
    assert kwargs.get("region") == "ap-southeast-2"
    # No credentials are passed: the AWS default chain must engage.
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
    assert "api_key" not in kwargs


def test_get_deepeval_config_reads_top_level_judge_block(monkeypatch):
    """get_deepeval_config reads the TOP-LEVEL global judge: block, not per-dataset."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    config = {
        "judge": {
            "enabled": True,
            "provider": "bedrock",
            "model": "au.anthropic.claude-opus-4-6",
            "temperature": 0.0,
            "max_concurrent": 5,
        },
        # A per-dataset deepeval block that MUST be ignored (the deleted bug path).
        "datasets": {
            "legal_rag_bench": {
                "deepeval": {
                    "judge_model": "gpt-4o",
                    "judge_model_provider": "openai",
                }
            }
        },
    }

    result = get_deepeval_config(config)

    assert result["judge_model"] == "au.anthropic.claude-opus-4-6"
    assert result["judge_model_provider"] == "bedrock"
    assert result["max_concurrent"] == 5
    assert result["region"] == "ap-southeast-2"


def test_get_deepeval_config_global_for_gst_and_legal_rag(monkeypatch):
    """Bug-fix: a gst_ slice gets the SAME judge config as legal_rag_bench.

    The judge is global; the former per-dataset read made gst_ slices borrow
    legal_rag_bench's judge block. With the global judge: block, the resolved
    judge config is identical regardless of which dataset slice is present.
    """
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    judge_block = {
        "judge": {
            "provider": "bedrock",
            "model": "au.anthropic.claude-opus-4-6",
        }
    }
    config_legal = {
        **judge_block,
        "datasets": {"legal_rag_bench": {"deepeval": {"judge_model": "gpt-4o"}}},
    }
    config_gst = {
        **judge_block,
        "datasets": {"gst_legal_rag": {"deepeval": {"judge_model": "gpt-3.5"}}},
    }

    result_legal = get_deepeval_config(config_legal)
    result_gst = get_deepeval_config(config_gst)

    # Same judge config for both slices (per-dataset blocks ignored).
    assert result_legal == result_gst
    assert result_gst["judge_model"] == "au.anthropic.claude-opus-4-6"
    assert result_gst["judge_model_provider"] == "bedrock"


def test_get_deepeval_config_env_wins_over_yaml(monkeypatch):
    """CRUCIBLE_JUDGE_* env overrides win over the YAML judge: block."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("CRUCIBLE_JUDGE_PROVIDER", "bedrock")
    monkeypatch.setenv("CRUCIBLE_JUDGE_MODEL", "au.anthropic.claude-sonnet-4-5")
    config = {
        "judge": {
            "provider": "openai",
            "model": "gpt-4o",
        }
    }

    result = get_deepeval_config(config)

    # Env wins over YAML for both provider and model.
    assert result["judge_model_provider"] == "bedrock"
    assert result["judge_model"] == "au.anthropic.claude-sonnet-4-5"


def test_get_deepeval_config_defaults_to_bedrock_opus(monkeypatch):
    """With nothing set, judge defaults to bedrock + the O1 judge model ID."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    config = {}  # no judge: block, no env overrides

    result = get_deepeval_config(config)

    assert result["judge_model_provider"] == "bedrock"
    assert result["judge_model"] == DEFAULT_JUDGE_MODEL == "au.anthropic.claude-opus-4-6"
    assert result["region"] == "ap-southeast-2"


def test_get_deepeval_config_cli_overrides_env_and_yaml(monkeypatch):
    """CLI args win over env and YAML (top of the precedence stack)."""
    monkeypatch.setenv("CRUCIBLE_JUDGE_PROVIDER", "bedrock")
    monkeypatch.setenv("CRUCIBLE_JUDGE_MODEL", "au.anthropic.claude-opus-4-6")
    config = {"judge": {"provider": "bedrock", "model": "au.anthropic.claude-opus-4-6"}}

    result = get_deepeval_config(
        config,
        cli_enabled=False,
        cli_judge_model="gpt-4o-mini",
        cli_provider="openai",
        cli_temperature=0.2,
        cli_max_concurrent=20,
    )

    assert result["enabled"] is False
    assert result["judge_model"] == "gpt-4o-mini"
    assert result["judge_model_provider"] == "openai"
    assert result["temperature"] == 0.2
    assert result["max_concurrent"] == 20
    # provider!=bedrock => region not resolved (no AWS_REGION needed).
    assert result["region"] is None
