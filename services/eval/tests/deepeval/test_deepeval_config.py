"""Tests for DeepEval configuration."""

import os
from unittest import mock

import pytest

from app.deepeval.bedrock_provider import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_TEMPERATURE,
    get_deepeval_config,
    get_deepeval_llm,
)

# Judge-related EVAL_* / AWS_* env vars touched by these tests. We clear
# them per-test so suite ordering cannot leak provider/model/region state.
_JUDGE_ENV_VARS = (
    "EVAL_JUDGE_PROVIDER",
    "EVAL_JUDGE_MODEL",
    "EVAL_GENERATOR_MODEL",
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
    assert DEFAULT_TEMPERATURE == 0.0
    assert DEFAULT_MAX_CONCURRENT == 10
    # Bedrock-only: DEFAULT_BEDROCK_MODEL is the resolved au.-profile judge default.
    assert DEFAULT_BEDROCK_MODEL == "au.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_openai_provider_rejected(monkeypatch):
    """Bedrock-only: get_deepeval_llm rejects provider=openai (gpt-4o removed)."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    with pytest.raises(ValueError, match="Bedrock-only"):
        get_deepeval_llm(provider="openai", model="gpt-4o-mini")


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
            model="au.anthropic.claude-haiku-4-5-20251001-v1:0",
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
            "model": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
            "temperature": 0.0,
            "max_concurrent": 5,
        },
        # A per-dataset deepeval block that MUST be ignored (the deleted bug path).
        "datasets": {
            "legal_rag_bench": {
                "deepeval": {
                    "judge_model": "IGNORED-per-dataset",
                    "judge_model_provider": "IGNORED",
                }
            }
        },
    }

    result = get_deepeval_config(config)

    assert result["judge_model"] == "au.anthropic.claude-haiku-4-5-20251001-v1:0"
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
            "model": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
        }
    }
    config_legal = {
        **judge_block,
        "datasets": {"legal_rag_bench": {"deepeval": {"judge_model": "IGNORED-a"}}},
    }
    config_gst = {
        **judge_block,
        "datasets": {"gst_legal_rag": {"deepeval": {"judge_model": "IGNORED-b"}}},
    }

    result_legal = get_deepeval_config(config_legal)
    result_gst = get_deepeval_config(config_gst)

    # Same judge config for both slices (per-dataset blocks ignored).
    assert result_legal == result_gst
    assert result_gst["judge_model"] == "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert result_gst["judge_model_provider"] == "bedrock"


def test_get_deepeval_config_env_wins_over_yaml(monkeypatch):
    """EVAL_JUDGE_* env overrides win over the YAML judge: block."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("EVAL_JUDGE_PROVIDER", "bedrock")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "au.anthropic.claude-sonnet-4-5")
    config = {
        "judge": {
            "provider": "bedrock",
            "model": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
        }
    }

    result = get_deepeval_config(config)

    # Env model wins over the YAML model (both Bedrock).
    assert result["judge_model_provider"] == "bedrock"
    assert result["judge_model"] == "au.anthropic.claude-sonnet-4-5"


def test_get_deepeval_config_defaults_to_bedrock_haiku(monkeypatch):
    """With nothing set, judge defaults to bedrock + the O1 judge model ID."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    config = {}  # no judge: block, no env overrides

    result = get_deepeval_config(config)

    assert result["judge_model_provider"] == "bedrock"
    assert result["judge_model"] == DEFAULT_JUDGE_MODEL == "au.anthropic.claude-sonnet-4-5-20250929-v1:0"  # noqa: E501
    assert result["region"] == "ap-southeast-2"


def test_get_deepeval_config_cli_overrides_env_and_yaml(monkeypatch):
    """CLI args win over env and YAML (top of the precedence stack). Bedrock-only."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "au.anthropic.claude-haiku-4-5-20251001-v1:0")
    config = {"judge": {"provider": "bedrock", "model": "au.anthropic.claude-haiku-4-5-20251001-v1:0"}}  # noqa: E501

    result = get_deepeval_config(
        config,
        cli_enabled=False,
        cli_judge_model="au.anthropic.claude-sonnet-4-5",
        cli_provider="bedrock",
        cli_temperature=0.2,
        cli_max_concurrent=20,
    )

    assert result["enabled"] is False
    assert result["judge_model"] == "au.anthropic.claude-sonnet-4-5"
    assert result["judge_model_provider"] == "bedrock"
    assert result["temperature"] == 0.2
    assert result["max_concurrent"] == 20
    assert result["region"] == "ap-southeast-2"
