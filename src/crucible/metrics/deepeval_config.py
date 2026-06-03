"""
DeepEval configuration for LLM-judge metrics.

This module provides configuration for DeepEval evaluation metrics including
LLM backend setup for OpenAI and AWS Bedrock providers.

Metrics supported:
- FaithfulnessMetric: Detects hallucinations in generated answers
- ContextualPrecisionMetric: Measures signal-to-noise in retrieved contexts
- ContextualRecallMetric: Evaluates coverage of relevant information
- AnswerRelevancyMetric: Assesses directness of response to question
"""

from __future__ import annotations

import os
from typing import Any, Final

from beartype import beartype
from beartype.typing import Dict
from dotenv import load_dotenv

# ====================================================================
# SECURITY: DISABLE THIRD-PARTY TELEMETRY
# ====================================================================
# This setting DISABLES DeepEval telemetry (analytics, usage stats, etc.).
#
# DO NOT REMOVE OR MODIFY THIS SETTING.
#
# Reasons:
# 1. Privacy: Evaluation runs may contain sensitive query data
# 2. Security: Telemetry sends data to external servers (confusingproxy.com)
# 3. Compliance: Many organizations prohibit external telemetry
# 4. Cost: Telemetry consumes bandwidth and may incur costs
#
# To verify telemetry is disabled, check:
#   - DeepEval source code should respect DEEPEVAL_TELEMETRY_OPT_OUT
#   - No outbound connections to confusingproxy.com or similar
#
# Reference: https://docs.confident-ai.com/docs/telemetry-opt-out
# ====================================================================
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"

# Load .env file if present (but telemetry opt-out above takes precedence)
load_dotenv()

# Constants
OPENAI_API_KEY_ENV: Final[str] = "OPENAI_API_KEY"
DEEPEVAL_MAX_CONCURRENT_ENV: Final[str] = "DEEPEVAL_MAX_CONCURRENT"
DEFAULT_OPENAI_MODEL: Final[str] = "gpt-4o-mini"
DEFAULT_TEMPERATURE: Final[float] = 0.0
DEFAULT_MAX_CONCURRENT: Final[int] = 10

# ====================================================================
# JUDGE PROVIDER / MODEL DEFAULTS (Bedrock-default; OpenAI opt-in)
# ====================================================================
# Bedrock is the default judge provider. The default judge model is an
# AU-geographic inference profile (au.*) so Australian legal/PII data stays
# in-country. Use au.* NOT apac.* (apac routes across the broad APAC region,
# a residency regression).
#
# HARD INVARIANT (non-negotiable): the judge model ID MUST differ from the
# generator model ID — no same-model self-grading. The two defaults below are
# Opus (judge) vs Sonnet (generator) precisely to satisfy this.
#
# IMPLEMENTATION-TIME ACTION: confirm the exact dated au.* strings AND account
# model-access against AWS "Supported Regions and models for inference
# profiles" for ap-southeast-2 before merging; these change.
DEFAULT_JUDGE_PROVIDER: Final[str] = "bedrock"
DEFAULT_JUDGE_MODEL: Final[str] = "au.anthropic.claude-opus-4-6"

# Kept in sync with crucible.stubs.rag.generator.DEFAULT_GENERATOR_MODEL.
# Used only to enforce the judge != generator invariant locally without
# importing the generator module (avoids a cross-module import for a constant).
DEFAULT_GENERATOR_MODEL: Final[str] = "au.anthropic.claude-sonnet-4-6"

# DEFAULT_BEDROCK_MODEL is the resolved au.-profile judge default. It replaces
# the former bare-family ID (anthropic.claude-3-5-sonnet-20241022-v2:0), which
# violated the inference-profile requirement (Showstopper A). Re-exported from
# crucible.metrics; keep that export in sync with this value.
DEFAULT_BEDROCK_MODEL: Final[str] = DEFAULT_JUDGE_MODEL


@beartype
def _get_openai_api_key() -> str:
    """
    Get OpenAI API key from environment.

    Returns:
        OpenAI API key string.

    Raises:
        ValueError: If OPENAI_API_KEY environment variable is not set.

    """
    api_key = os.environ.get(OPENAI_API_KEY_ENV)
    if not api_key:
        raise ValueError(
            f"{OPENAI_API_KEY_ENV} environment variable must be set to use DeepEval. "
            "Set it with: export OPENAI_API_KEY=your-key-here"
        )
    return api_key


def _looks_like_bedrock_model(model: str) -> bool:
    """
    Dev-only convenience sniff for whether a model ID looks like a Bedrock model.

    Recognises geographic inference-profile prefixes (us./eu./apac./au./global.)
    AND bare family prefixes (anthropic./amazon./...). An explicit
    CRUCIBLE_JUDGE_PROVIDER is the correctness path; this only powers the
    fail-loud provider/model disagreement check.
    """
    return model.startswith(
        (
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
    )


def _resolve_judge_provider_and_model(
    yaml_provider: str | None = None,
    yaml_model: str | None = None,
) -> tuple[str, str]:
    """
    Resolve the GLOBAL judge (provider, model) with env > YAML > default.

    The judge is the measuring instrument, so these vars are global across all
    datasets. Contract:
    - CRUCIBLE_JUDGE_PROVIDER: "bedrock" (default) or "openai".
    - CRUCIBLE_JUDGE_MODEL: model ID (inference-profile ID for bedrock).
    - Precedence env > YAML > default for both provider and model.
    - Explicit provider WINS; FAIL LOUD on provider/model disagreement.
    - HARD INVARIANT: the resolved judge model ID MUST NOT equal the generator
      model ID (no same-model self-grading); raise if equal.

    Args:
        yaml_provider: Provider from the top-level YAML judge block (if any).
        yaml_model: Model from the top-level YAML judge block (if any).

    Returns:
        (provider, model) tuple.

    Raises:
        ValueError: On unsupported provider, provider/model disagreement, or a
            judge == generator model collision.

    """
    # Resolve model: env > YAML > default.
    model = os.getenv("CRUCIBLE_JUDGE_MODEL")
    if model is None:
        model = yaml_model if yaml_model is not None else DEFAULT_JUDGE_MODEL

    # Resolve provider: env > YAML > default.
    env_provider = os.getenv("CRUCIBLE_JUDGE_PROVIDER")
    if env_provider is not None:
        provider = env_provider.strip().lower()
    elif yaml_provider is not None:
        provider = yaml_provider.strip().lower()
    else:
        provider = DEFAULT_JUDGE_PROVIDER

    if provider not in ("bedrock", "openai"):
        raise ValueError(f"Unsupported judge provider: {provider!r}. Use 'bedrock' or 'openai'.")

    # Explicit provider WINS, but FAIL LOUD on provider/model disagreement.
    model_looks_bedrock = _looks_like_bedrock_model(model)
    if provider == "bedrock" and not model_looks_bedrock:
        raise ValueError(
            f"Judge provider=bedrock disagrees with judge model={model!r} (does "
            "not look like a Bedrock model/inference-profile ID). Fix the model "
            "ID or the provider; refusing to silently pick."
        )
    if provider == "openai" and model_looks_bedrock:
        raise ValueError(
            f"Judge provider=openai disagrees with judge model={model!r} (looks "
            "like a Bedrock model ID). Fix the model ID or the provider; "
            "refusing to silently pick."
        )

    # HARD INVARIANT: judge model != generator model (no self-grading).
    generator_model = os.getenv("CRUCIBLE_GENERATOR_MODEL", DEFAULT_GENERATOR_MODEL)
    if model == generator_model:
        raise ValueError(
            f"Judge model ({model!r}) must not equal the generator model "
            f"({generator_model!r}); same-model self-grading is not allowed. "
            "Use a different judge model (e.g. au.anthropic.claude-opus-4-6 as "
            "judge with au.anthropic.claude-sonnet-4-6 as generator)."
        )

    return provider, model


def _resolve_bedrock_region() -> str:
    """
    Resolve the AWS region for a Bedrock judge, env > YAML, one source of truth.

    boto3 / AmazonBedrockModel read AWS_REGION / AWS_DEFAULT_REGION natively; we
    do NOT reimplement resolution, but we read the same vars to determine the
    region_name to pass explicitly to AmazonBedrockModel and to fail loud when
    it is unset (no implicit us-east-1 default — a residency hazard).

    Returns:
        The resolved region name.

    Raises:
        ValueError: If neither AWS_REGION nor AWS_DEFAULT_REGION is set.

    """
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if not region:
        raise ValueError(
            "AWS region is not set for a Bedrock judge run. Set AWS_REGION (or "
            "AWS_DEFAULT_REGION) to a region matching your inference-profile "
            "geography (e.g. ap-southeast-2 for au.* profiles). Refusing to "
            "default to us-east-1 (data-residency hazard)."
        )
    return region


@beartype
def get_deepeval_llm(
    provider: str = "openai",
    model: str = DEFAULT_OPENAI_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
) -> Any:
    """
    Get LLM backend for DeepEval evaluation.

    Args:
        provider: LLM provider name ("openai" or "bedrock"). Default: "openai".
        model: Model name. Default: gpt-4o-mini.
        temperature: Sampling temperature. Default: 0.0.

    Returns:
        DeepEval-compatible LLM instance.

    Raises:
        ValueError: If provider is not supported or API key is missing.

    """
    if provider == "openai":
        from deepeval.models import GPTModel

        api_key = _get_openai_api_key()
        return GPTModel(model=model, api_key=api_key, temperature=temperature)
    elif provider == "bedrock":
        from deepeval.models import AmazonBedrockModel

        # Resolve the AWS region explicitly (env > YAML, fail loud if unset) and
        # pass it to AmazonBedrockModel. No credentials are passed: the AWS
        # default credential chain engages when creds are omitted (do NOT copy
        # the OpenAI embedder's api_key= pattern onto Bedrock).
        region = _resolve_bedrock_region()
        # NOTE: AmazonBedrockModel has no `temperature` param -- extra kwargs leak
        # into the aiobotocore client constructor (AioSession._create_client) and
        # raise. Temperature belongs in `generation_kwargs`, which deepeval spreads
        # into the Converse API `inferenceConfig`.
        return AmazonBedrockModel(
            model=model,
            region=region,
            generation_kwargs={"temperature": temperature},
        )
    else:
        raise ValueError(f"Unsupported provider: {provider}. Use 'openai' or 'bedrock'.")


@beartype
def create_deepeval_metrics(
    llm_provider: str = "openai",
    judge_model: str = DEFAULT_OPENAI_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    embedder: Any = None,
) -> Dict[str, Any]:
    """
    Create DeepEval metrics with configured LLM backend.

    Args:
        llm_provider: LLM provider ("openai" or "bedrock"). Default: "openai".
        judge_model: Judge model name. Default: gpt-4o-mini.
        temperature: Sampling temperature. Default: 0.0.
        embedder: Optional shared embedder instance (for future use).

    Returns:
        Dictionary mapping metric names to instantiated DeepEval metric objects.

    Raises:
        ValueError: If provider is not supported or API key is missing.

    """
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        FaithfulnessMetric,
    )

    # Get LLM backend
    llm = get_deepeval_llm(
        provider=llm_provider,
        model=judge_model,
        temperature=temperature,
    )

    # NOTE: DeepEval v4 handles embeddings internally for AnswerRelevancyMetric
    # The embedder parameter is kept for API compatibility but not currently used
    _ = embedder  # noqa: F841

    # Create metrics with LLM backend
    # In DeepEval v4, we pass the model as a string or LLM instance
    # include_reason=True ensures reason attribute is populated after measure()
    return {
        "faithfulness": FaithfulnessMetric(model=llm, include_reason=True),
        "context_precision": ContextualPrecisionMetric(model=llm, include_reason=True),
        "context_recall": ContextualRecallMetric(model=llm, include_reason=True),
        "answer_relevancy": AnswerRelevancyMetric(model=llm, include_reason=True),
    }


@beartype
def get_deepeval_config(
    config: dict[str, Any],
    cli_enabled: bool | None = None,
    cli_judge_model: str | None = None,
    cli_provider: str | None = None,
    cli_temperature: float | None = None,
    cli_max_concurrent: int | None = None,
) -> dict[str, Any]:
    """
    Get the GLOBAL DeepEval judge configuration from multiple sources.

    The judge is the measuring instrument, so its config is GLOBAL across all
    datasets. This function reads a TOP-LEVEL global ``judge:`` block from the
    config dict (NOT the former per-dataset ``datasets.legal_rag_bench.deepeval``
    path). The former per-dataset read was a latent bug: it applied
    ``legal_rag_bench``'s judge block even for ``gst_`` slices ("global by
    accident, mislabeled"). Reading the top-level ``judge:`` block makes the
    judge global on purpose and fixes that bug.

    Expected top-level ``judge:`` block shape (all keys optional; Task Group 7
    writes this block into eval_config.yaml):
        judge:
          enabled: bool          # default True
          provider: str          # "bedrock" (default) or "openai"
          model: str             # judge model ID (inference-profile for bedrock)
          temperature: float     # default 0.0
          max_concurrent: int    # default 10

    Precedence:
    - provider/model: CLI > env (CRUCIBLE_JUDGE_*) > YAML > default, with the
      explicit-provider-WINS fail-loud disagreement check and the judge !=
      generator invariant (delegated to _resolve_judge_provider_and_model).
    - enabled/temperature: CLI > YAML > default.
    - max_concurrent: CLI > env (DEEPEVAL_MAX_CONCURRENT) > YAML > default.
    - region: resolved (env > YAML, fail loud if unset) only when the resolved
      provider is bedrock; returned under the ``region`` key for the bedrock
      client wiring in get_deepeval_llm.

    Args:
        config: Loaded configuration dictionary from eval_config.yaml.
        cli_enabled: CLI flag for enabling/disabling DeepEval.
        cli_judge_model: CLI-specified judge model name (wins over env/YAML).
        cli_provider: CLI-specified LLM provider (wins over env/YAML).
        cli_temperature: CLI-specified temperature.
        cli_max_concurrent: CLI-specified max concurrent evaluations.

    Returns:
        Dictionary with DeepEval configuration keys:
            - enabled: bool
            - judge_model: str
            - judge_model_provider: str
            - temperature: float
            - max_concurrent: int
            - region: str | None  (the resolved region for a bedrock judge)

    Raises:
        ValueError: On unsupported provider, provider/model disagreement, a
            judge == generator collision, or an unset region for a bedrock judge.

    """
    # Read the TOP-LEVEL global judge block (NOT the per-dataset path). The old
    # datasets.legal_rag_bench.deepeval read path is deleted on purpose: the
    # judge is the measuring instrument and is global across all datasets.
    judge_config = config.get("judge", {}) if isinstance(config, dict) else {}

    # Resolve enabled flag (CLI > YAML > default).
    if cli_enabled is not None:
        enabled = cli_enabled
    else:
        enabled = judge_config.get("enabled", True)

    # Resolve provider + judge_model together: CLI > env (CRUCIBLE_JUDGE_*) >
    # YAML > default. CLI wins outright when supplied; otherwise the shared
    # helper applies env > YAML > default with the explicit-provider-WINS
    # fail-loud disagreement check and the judge != generator invariant.
    yaml_provider = judge_config.get("provider")
    yaml_model = judge_config.get("model")
    resolved_provider, resolved_model = _resolve_judge_provider_and_model(
        yaml_provider=yaml_provider,
        yaml_model=yaml_model,
    )
    provider = cli_provider if cli_provider is not None else resolved_provider
    judge_model = cli_judge_model if cli_judge_model is not None else resolved_model

    # Resolve temperature (CLI > YAML > default).
    if cli_temperature is not None:
        temperature = cli_temperature
    else:
        temperature = judge_config.get("temperature", DEFAULT_TEMPERATURE)

    # Resolve max_concurrent (CLI > env var > YAML > default).
    if cli_max_concurrent is not None:
        max_concurrent = cli_max_concurrent
    else:
        env_max_concurrent = os.environ.get(DEEPEVAL_MAX_CONCURRENT_ENV)
        if env_max_concurrent:
            try:
                max_concurrent = int(env_max_concurrent)
            except ValueError:
                max_concurrent = judge_config.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
        else:
            max_concurrent = judge_config.get("max_concurrent", DEFAULT_MAX_CONCURRENT)

    # Resolve region only for a bedrock judge (fail loud if unset). One source
    # of truth: env (AWS_REGION/AWS_DEFAULT_REGION) > YAML, no implicit default.
    region = _resolve_bedrock_region() if provider == "bedrock" else None

    return {
        "enabled": enabled,
        "judge_model": judge_model,
        "judge_model_provider": provider,
        "temperature": temperature,
        "max_concurrent": max_concurrent,
        "region": region,
    }
