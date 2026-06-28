"""
Tests for the inline provider/model/region resolution contract (Task Group 2).

These cover the Bedrock-default env-var contract that is implemented INLINE in
the generator and the deepeval-config modules (there is intentionally no shared
provider-resolution module):

- crucible.local.stubs.rag.generator._resolve_generator_provider_and_model / _resolve_region
- crucible.service.deepeval.bedrock_provider._resolve_judge_provider_and_model
  / _resolve_bedrock_region

unittest.mock.patch.dict isolates os.environ per test.
"""

import os
from unittest import mock

import pytest

from crucible.local.stubs.rag.generator import (
    DEFAULT_GENERATOR_MODEL,
    _resolve_generator_provider_and_model,
    _resolve_region,
)
from crucible.service.deepeval.bedrock_provider import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_JUDGE_MODEL,
    _resolve_bedrock_region,
    _resolve_judge_provider_and_model,
)

# Env keys that influence resolution; cleared per test for a clean baseline.
_RESOLUTION_ENV_KEYS = [
    "CRUCIBLE_GENERATOR_PROVIDER",
    "CRUCIBLE_GENERATOR_MODEL",
    "CRUCIBLE_JUDGE_PROVIDER",
    "CRUCIBLE_JUDGE_MODEL",
    "RAG_GENERATOR_MODEL",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
]


class _patched_env:
    """Context manager: clear all resolution env keys, then apply overrides."""

    def __init__(self, **overrides):
        self._desired = dict(overrides)
        self._removes = [k for k in _RESOLUTION_ENV_KEYS if k not in self._desired]
        self._outer = mock.patch.dict("os.environ", self._desired, clear=False)
        self._saved: dict[str, str] = {}

    def __enter__(self):
        self._outer.__enter__()
        for key in self._removes:
            if key in os.environ:
                self._saved[key] = os.environ.pop(key)
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            os.environ[key] = value
        self._outer.__exit__(*exc)
        return False


def test_defaults_resolve_to_bedrock_and_o1_model_ids():
    """With nothing set, generator + judge default to bedrock + the au.* O1 IDs."""
    with _patched_env():
        gen_provider, gen_model = _resolve_generator_provider_and_model()
        judge_provider, judge_model = _resolve_judge_provider_and_model()

    assert gen_provider == "bedrock"
    assert gen_model == "au.anthropic.claude-sonnet-4-6"
    assert gen_model == DEFAULT_GENERATOR_MODEL

    assert judge_provider == "bedrock"
    assert judge_model == "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert judge_model == DEFAULT_JUDGE_MODEL
    # Replaced constant + export flows the au.-profile judge default.
    assert DEFAULT_BEDROCK_MODEL == DEFAULT_JUDGE_MODEL
    # HARD INVARIANT: judge model != generator model.
    assert judge_model != gen_model


def test_env_beats_yaml_for_judge_provider_model_and_region():
    """env > YAML for provider, model, and region."""
    with _patched_env(
        CRUCIBLE_JUDGE_PROVIDER="openai",
        CRUCIBLE_JUDGE_MODEL="gpt-4o",
        AWS_REGION="ap-southeast-2",
    ):
        provider, model = _resolve_judge_provider_and_model(
            yaml_provider="bedrock", yaml_model="au.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        region = _resolve_bedrock_region()

    assert provider == "openai"  # env wins over YAML bedrock
    assert model == "gpt-4o"  # env wins over YAML model
    assert region == "ap-southeast-2"


def test_explicit_provider_wins_fails_loud_on_disagreement():
    """provider=bedrock with an OpenAI-looking model ID fails loud (no silent pick)."""
    with _patched_env(
        CRUCIBLE_GENERATOR_PROVIDER="bedrock",
        CRUCIBLE_GENERATOR_MODEL="gpt-4o-mini",
    ):
        with pytest.raises(ValueError, match="disagrees"):
            _resolve_generator_provider_and_model()

    with _patched_env(
        CRUCIBLE_JUDGE_PROVIDER="bedrock",
        CRUCIBLE_JUDGE_MODEL="gpt-4o-mini",
    ):
        with pytest.raises(ValueError, match="disagrees"):
            _resolve_judge_provider_and_model()


def test_old_rag_generator_model_without_crucible_raises_rename_error():
    """RAG_GENERATOR_MODEL set + CRUCIBLE_GENERATOR_MODEL unset raises; never silent."""
    with _patched_env(RAG_GENERATOR_MODEL="gpt-4o-mini"):
        with pytest.raises(
            ValueError,
            match="RAG_GENERATOR_MODEL is renamed to CRUCIBLE_GENERATOR_MODEL",
        ):
            _resolve_generator_provider_and_model()


def test_region_unset_on_bedrock_run_fails_loud():
    """No AWS_REGION / AWS_DEFAULT_REGION on a bedrock run fails loud (no us-east-1)."""
    with _patched_env():
        with pytest.raises(ValueError, match="AWS region is not set"):
            _resolve_region()
        with pytest.raises(ValueError, match="AWS region is not set"):
            _resolve_bedrock_region()


def test_judge_equals_generator_model_is_rejected():
    """HARD INVARIANT: judge model ID must not equal the generator model ID."""
    with _patched_env(
        CRUCIBLE_GENERATOR_MODEL="au.anthropic.claude-haiku-4-5-20251001-v1:0",
        CRUCIBLE_JUDGE_MODEL="au.anthropic.claude-haiku-4-5-20251001-v1:0",
    ):
        with pytest.raises(ValueError, match="must not equal the generator model"):
            _resolve_judge_provider_and_model()


def test_au_profile_model_not_misclassified_as_openai():
    """_is_bedrock_model recognises au.* geo-profile prefixes (dev-only sniff)."""
    from crucible.local.stubs.rag.generator import _is_bedrock_model

    assert _is_bedrock_model("au.anthropic.claude-sonnet-4-6") is True
    assert _is_bedrock_model("us.anthropic.claude-3-5-sonnet") is True
    assert _is_bedrock_model("global.anthropic.claude") is True
    assert _is_bedrock_model("gpt-4o-mini") is False
