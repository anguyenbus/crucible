"""
DeepEval LLM-judge provider (impure, service-side).

This module holds the IMPURE half of the former ``metrics/deepeval_config.py``:
the env reads, boto/session wiring, judge provider/model/region
resolution, the concurrency env read, and the DeepEval metric factory. It imports
the PURE constants and the ``assert_distinct`` self-grading guard BACK from the
kernel (``crucible.kernel.rag_metrics.metric_specs``) -- the kernel remains the
single source of truth for the au.* constants and the judge != generator
invariant (service-ish -> kernel is allowed; the kernel never imports this
module, so there is no cycle).

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

# Phase 2: the pure metric specs (default judge/generator IDs, numeric defaults,
# the bedrock-model sniff, and the judge != generator guard) live in the kernel.
# The impure resolvers below import them BACK from there
# (service -> kernel is allowed; the kernel never imports this module).
from crucible.kernel.rag_metrics.metric_specs import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_GENERATOR_MODEL,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_JUDGE_PROVIDER,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_TEMPERATURE,
    _looks_like_bedrock_model,
    assert_distinct,
)

# Constants (impure / service-side resolvers' configuration).
# This project is BEDROCK-ONLY. The judge always runs on AWS Bedrock; there is no
# other provider, key, or default model.
DEEPEVAL_MAX_CONCURRENT_ENV: Final[str] = "DEEPEVAL_MAX_CONCURRENT"

# Re-exported for back-compat (these now originate in the kernel metric_specs).
__all__ = [
    "DEFAULT_BEDROCK_MODEL",
    "DEFAULT_GENERATOR_MODEL",
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_JUDGE_PROVIDER",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_TEMPERATURE",
    "DEEPEVAL_MAX_CONCURRENT_ENV",
    "create_deepeval_metrics",
    "get_deepeval_config",
    "get_deepeval_llm",
]


def _resolve_judge_provider_and_model(
    yaml_provider: str | None = None,
    yaml_model: str | None = None,
) -> tuple[str, str]:
    """
    Resolve the GLOBAL judge (provider, model) with env > YAML > default.

    The judge is the measuring instrument, so these vars are global across all
    datasets. Contract:
    - CRUCIBLE_JUDGE_PROVIDER: "bedrock" (the only supported provider).
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

    if provider != "bedrock":
        raise ValueError(
            f"Unsupported judge provider: {provider!r}. This project is Bedrock-only; "
            "use 'bedrock'."
        )

    # FAIL LOUD if the model id does not look like a Bedrock inference profile.
    if not _looks_like_bedrock_model(model):
        raise ValueError(
            f"Judge provider=bedrock disagrees with judge model={model!r} (does "
            "not look like a Bedrock model/inference-profile ID). Fix the model "
            "ID; this project is Bedrock-only."
        )

    # HARD INVARIANT: judge model != generator model (no self-grading). The pure
    # check now lives in the kernel (assert_distinct).
    generator_model = os.getenv("CRUCIBLE_GENERATOR_MODEL", DEFAULT_GENERATOR_MODEL)
    assert_distinct(model, generator_model)

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
    provider: str = "bedrock",
    model: str = DEFAULT_JUDGE_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
) -> Any:
    """
    Get the AWS Bedrock LLM backend for DeepEval evaluation.

    This project is Bedrock-only. ``provider`` is kept for config-shape
    compatibility but must be ``"bedrock"``.

    Args:
        provider: Must be "bedrock".
        model: Bedrock inference-profile id. Default: the kernel judge default.
        temperature: Sampling temperature. Default: 0.0.

    Returns:
        A DeepEval ``AmazonBedrockModel`` instance.

    Raises:
        ValueError: If provider is not "bedrock", or the AWS region is unset.

    """
    if provider != "bedrock":
        raise ValueError(
            f"Unsupported provider: {provider!r}. This project is Bedrock-only; "
            "use 'bedrock'."
        )
    from deepeval.models import AmazonBedrockModel

    # Resolve the AWS region explicitly (env > YAML, fail loud if unset) and pass
    # it to AmazonBedrockModel. No credentials are passed: the AWS default
    # credential chain engages when creds are omitted.
    region = _resolve_bedrock_region()
    # NOTE: AmazonBedrockModel has no `temperature` param -- extra kwargs leak into
    # the aiobotocore client constructor and raise. Temperature belongs in
    # `generation_kwargs`, which deepeval spreads into the Converse `inferenceConfig`.
    return AmazonBedrockModel(
        model=model,
        region=region,
        generation_kwargs={"temperature": temperature},
    )


@beartype
def create_deepeval_metrics(
    llm_provider: str = "bedrock",
    judge_model: str = DEFAULT_JUDGE_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    embedder: Any = None,
) -> Dict[str, Any]:
    """
    Create DeepEval metrics with the AWS Bedrock judge backend.

    Args:
        llm_provider: Must be "bedrock" (this project is Bedrock-only).
        judge_model: Bedrock judge inference-profile id. Default: kernel judge default.
        temperature: Sampling temperature. Default: 0.0.
        embedder: Optional shared embedder instance (for future use).

    Returns:
        Dictionary mapping metric names to instantiated DeepEval metric objects.

    Raises:
        ValueError: If provider is not "bedrock", or the AWS region is unset.

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
          provider: str          # "bedrock" (the only supported provider)
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
