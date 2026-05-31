"""Tests for DeepEval configuration."""

import os

import pytest

from crucible.metrics.deepeval_config import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_TEMPERATURE,
    get_deepeval_config,
)


def test_deepeval_telemetry_disabled():
    """Test that DeepEval telemetry is disabled."""
    assert os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") == "YES"


def test_constants():
    """Test module constants."""
    assert DEFAULT_OPENAI_MODEL == "gpt-4o-mini"
    assert DEFAULT_TEMPERATURE == 0.0
    assert DEFAULT_MAX_CONCURRENT == 10
    assert DEFAULT_BEDROCK_MODEL == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_get_openai_api_key_missing():
    """Test that missing API key raises ValueError."""
    from crucible.metrics.deepeval_config import _get_openai_api_key

    # Ensure env var is not set
    if "OPENAI_API_KEY" in os.environ:
        del os.environ["OPENAI_API_KEY"]

    with pytest.raises(ValueError, match="OPENAI_API_KEY.*must be set"):
        _get_openai_api_key()


def test_get_openai_api_key_from_env():
    """Test API key from environment."""
    from crucible.metrics.deepeval_config import _get_openai_api_key

    os.environ["OPENAI_API_KEY"] = "test-key"
    api_key = _get_openai_api_key()
    assert api_key == "test-key"
    del os.environ["OPENAI_API_KEY"]


def test_get_deepeval_config_defaults():
    """Test DeepEval config with defaults."""
    config = {
        "datasets": {
            "legal_rag_bench": {
                "deepeval": {}
            }
        }
    }

    result = get_deepeval_config(config)

    assert result["enabled"] is True
    assert result["judge_model"] == DEFAULT_OPENAI_MODEL
    assert result["judge_model_provider"] == "openai"
    assert result["temperature"] == DEFAULT_TEMPERATURE
    assert result["max_concurrent"] == DEFAULT_MAX_CONCURRENT


def test_get_deepeval_config_from_yaml():
    """Test DeepEval config from YAML."""
    config = {
        "datasets": {
            "legal_rag_bench": {
                "deepeval": {
                    "enabled": False,
                    "judge_model": "gpt-4o",
                    "judge_model_provider": "openai",
                    "temperature": 0.5,
                    "max_concurrent": 5,
                }
            }
        }
    }

    result = get_deepeval_config(config)

    assert result["enabled"] is False
    assert result["judge_model"] == "gpt-4o"
    assert result["temperature"] == 0.5
    assert result["max_concurrent"] == 5


def test_get_deepeval_config_cli_overrides():
    """Test CLI parameter overrides."""
    config = {
        "datasets": {
            "legal_rag_bench": {
                "deepeval": {
                    "enabled": False,
                    "judge_model": "gpt-4o",
                }
            }
        }
    }

    result = get_deepeval_config(
        config,
        cli_enabled=True,
        cli_judge_model="gpt-4o-mini",
        cli_provider="bedrock",
        cli_temperature=0.1,
        cli_max_concurrent=20,
    )

    assert result["enabled"] is True  # CLI override
    assert result["judge_model"] == "gpt-4o-mini"  # CLI override
    assert result["judge_model_provider"] == "bedrock"  # CLI override
    assert result["temperature"] == 0.1  # CLI override
    assert result["max_concurrent"] == 20  # CLI override


def test_get_deepeval_config_env_max_concurrent():
    """Test max_concurrent from environment variable."""
    os.environ["DEEPEVAL_MAX_CONCURRENT"] = "15"

    config = {"datasets": {"legal_rag_bench": {"deepeval": {}}}}
    result = get_deepeval_config(config)

    assert result["max_concurrent"] == 15
    del os.environ["DEEPEVAL_MAX_CONCURRENT"]
