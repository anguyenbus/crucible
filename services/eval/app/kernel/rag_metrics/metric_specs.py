"""
Pure RAG/judge metric specifications.

This module holds the infra-free metric specs that copy verbatim into the
monorepo kernel: the default judge/generator model IDs, numeric defaults, the
metric-name registry, default thresholds, the ``_looks_like_bedrock_model``
sniff, and the pure ``assert_distinct`` self-grading guard.

It imports NOTHING impure (no ``os``, no ``deepeval``, no ``dotenv``). The impure
resolvers in ``app.deepeval.bedrock_provider`` import these names BACK from
here (service-ish -> kernel is allowed; the kernel never imports deepeval_config,
so there is no cycle).
"""

from __future__ import annotations

from beartype.typing import Final

# ====================================================================
# JUDGE PROVIDER / MODEL DEFAULTS (Bedrock-only)
# ====================================================================
# Bedrock is the default judge provider. The default judge model is an
# AU-geographic inference profile (au.*) so Australian legal/PII data stays
# in-country. Use au.* NOT apac.* (apac routes across the broad APAC region,
# a residency regression).
#
# HARD INVARIANT (non-negotiable): the judge model ID MUST differ from the
# generator model ID -- no same-model self-grading. Policy: sonnet/haiku only,
# never opus. The two defaults below are Sonnet 4.5 (judge) vs Haiku 4.5
# (generator) precisely to satisfy this (both accept the `temperature` param).
DEFAULT_JUDGE_PROVIDER: Final[str] = "bedrock"
DEFAULT_JUDGE_MODEL: Final[str] = "au.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Kept in sync with the demo generator's default model.
# Used only to enforce the judge != generator invariant locally without
# importing the generator module (avoids a cross-module import for a constant).
DEFAULT_GENERATOR_MODEL: Final[str] = "au.anthropic.claude-sonnet-4-6"

# DEFAULT_BEDROCK_MODEL is the resolved au.-profile judge default. It replaces
# the former bare-family ID (anthropic.claude-3-5-sonnet-20241022-v2:0), which
# violated the inference-profile requirement (Showstopper A). Re-exported from
# app.deepeval.bedrock_provider; keep that export in sync with this value.
DEFAULT_BEDROCK_MODEL: Final[str] = DEFAULT_JUDGE_MODEL

# Numeric defaults (pure).
DEFAULT_TEMPERATURE: Final[float] = 0.0
DEFAULT_MAX_CONCURRENT: Final[int] = 10

# Metric-name registry: the four LLM-judge metrics the evaluator computes, in a
# stable order. The evaluator and metric factory key off these names.
METRIC_NAMES: Final[tuple[str, ...]] = (
    "faithfulness",
    "context_precision",
    "context_recall",
    "answer_relevancy",
)

# Default pass thresholds per metric (practical-significance floors). Pure data;
# callers may override. Kept conservative and uniform pending re-baseline.
DEFAULT_METRIC_THRESHOLDS: Final[dict[str, float]] = {
    "faithfulness": 0.7,
    "context_precision": 0.7,
    "context_recall": 0.7,
    "answer_relevancy": 0.7,
}

# Geographic inference-profile prefixes AND bare family prefixes recognised by
# the dev-only Bedrock model sniff.
_BEDROCK_MODEL_PREFIXES: Final[tuple[str, ...]] = (
    "us.",
    "eu.",
    "apac.",
    "au.",
    "global.",
    "anthropic.",
    "amazon.",
    "meta.",
    "mistral.",
    "cohere.",
)


def _looks_like_bedrock_model(model: str) -> bool:
    """
    Dev-only convenience sniff for whether a model ID looks like a Bedrock model.

    Recognises geographic inference-profile prefixes (us./eu./apac./au./global.)
    AND bare family prefixes (anthropic./amazon./...). An explicit
    CRUCIBLE_JUDGE_PROVIDER is the correctness path; this only powers the
    fail-loud provider/model disagreement check.

    Args:
        model: Model ID string.

    Returns:
        True if the ID starts with a recognised Bedrock prefix.

    """
    return model.startswith(_BEDROCK_MODEL_PREFIXES)


def assert_distinct(judge_id: str, generator_id: str) -> None:
    """
    Enforce the judge != generator hard invariant (no same-model self-grading).

    Args:
        judge_id: Resolved judge model ID.
        generator_id: Resolved generator model ID.

    Raises:
        ValueError: If the two IDs are equal.

    """
    if judge_id == generator_id:
        raise ValueError(
            f"Judge model ({judge_id!r}) must not equal the generator model "
            f"({generator_id!r}); same-model self-grading is not allowed. "
            "Use a different judge model (e.g. au.anthropic.claude-haiku-4-5-20251001-v1:0 as "
            "judge with au.anthropic.claude-sonnet-4-6 as generator)."
        )
