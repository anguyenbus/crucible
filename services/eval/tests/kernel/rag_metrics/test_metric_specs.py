"""Test-first (RED) coverage for the kernel rag_metrics pure specs.

Targets the FUTURE kernel path ``app.kernel.rag_metrics.metric_specs``
(Commit 5 extracts the pure constants + assert_distinct there). Until then these
tests fail at import/collection time.

Covers:
- the new pure ``assert_distinct(judge_id, generator_id)`` (distinct ok / equal
  raises) extracted from the inline judge != generator self-grading guard.
- the au.* judge/generator defaults and the other pure constants.

Deterministic fixtures only (NO hypothesis).
"""

from __future__ import annotations

import pytest

from app.kernel.rag_metrics.metric_specs import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_GENERATOR_MODEL,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_JUDGE_PROVIDER,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_TEMPERATURE,
    assert_distinct,
)


def test_assert_distinct_allows_different_judge_and_generator() -> None:
    """Distinct judge and generator IDs pass without raising."""
    # Returns None (no raise) for the supported Opus-judge / Sonnet-generator pair.
    assert assert_distinct(DEFAULT_JUDGE_MODEL, DEFAULT_GENERATOR_MODEL) is None


def test_assert_distinct_rejects_self_grading() -> None:
    """Equal judge and generator IDs raise (no same-model self-grading)."""
    same = "au.anthropic.claude-opus-4-6"
    with pytest.raises(ValueError):
        assert_distinct(same, same)


def test_au_defaults_are_in_country_inference_profiles() -> None:
    """Judge and generator defaults are au.* inference profiles, judge != generator."""
    assert DEFAULT_JUDGE_PROVIDER == "bedrock"
    assert DEFAULT_JUDGE_MODEL == "au.anthropic.claude-opus-4-6"
    assert DEFAULT_GENERATOR_MODEL == "au.anthropic.claude-sonnet-4-6"
    # DEFAULT_BEDROCK_MODEL is the resolved au.* judge default.
    assert DEFAULT_BEDROCK_MODEL == DEFAULT_JUDGE_MODEL
    # The hard invariant baked into the defaults: judge != generator.
    assert DEFAULT_JUDGE_MODEL != DEFAULT_GENERATOR_MODEL


def test_numeric_defaults() -> None:
    """The pure numeric defaults carry over verbatim."""
    assert DEFAULT_TEMPERATURE == 0.0
    assert DEFAULT_MAX_CONCURRENT == 10
