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
DEFAULT_BEDROCK_MODEL: Final[str] = "anthropic.claude-3-5-sonnet-20241022-v2:0"


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

        # Use AWS credential chain for Bedrock
        return AmazonBedrockModel(model=model, temperature=temperature)
    else:
        raise ValueError(
            f"Unsupported provider: {provider}. Use 'openai' or 'bedrock'."
        )


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
    Get DeepEval configuration from multiple sources with precedence.

    Precedence order (highest to lowest):
    1. CLI arguments (cli_* parameters)
    2. Environment variables
    3. YAML config (deepeval section from config dict)
    4. Defaults

    Args:
        config: Loaded configuration dictionary from eval_config.yaml.
        cli_enabled: CLI flag for enabling/disabling DeepEval.
        cli_judge_model: CLI-specified judge model name.
        cli_provider: CLI-specified LLM provider.
        cli_temperature: CLI-specified temperature.
        cli_max_concurrent: CLI-specified max concurrent evaluations.

    Returns:
        Dictionary with DeepEval configuration keys:
            - enabled: bool
            - judge_model: str
            - judge_model_provider: str
            - temperature: float
            - max_concurrent: int

    """
    # Get deepeval section from YAML config
    deepeval_config = (
        config.get("datasets", {}).get("legal_rag_bench", {}).get("deepeval", {})
    )

    # Resolve enabled flag (CLI > YAML > default)
    if cli_enabled is not None:
        enabled = cli_enabled
    else:
        enabled = deepeval_config.get("enabled", True)

    # Resolve judge_model (CLI > YAML > default)
    if cli_judge_model is not None:
        judge_model = cli_judge_model
    else:
        judge_model = deepeval_config.get("judge_model", DEFAULT_OPENAI_MODEL)

    # Resolve provider (CLI > YAML > default)
    if cli_provider is not None:
        provider = cli_provider
    else:
        provider = deepeval_config.get("judge_model_provider", "openai")

    # Resolve temperature (CLI > YAML > default)
    if cli_temperature is not None:
        temperature = cli_temperature
    else:
        temperature = deepeval_config.get("temperature", DEFAULT_TEMPERATURE)

    # Resolve max_concurrent (CLI > env var > YAML > default)
    if cli_max_concurrent is not None:
        max_concurrent = cli_max_concurrent
    else:
        # Check environment variable first
        env_max_concurrent = os.environ.get(DEEPEVAL_MAX_CONCURRENT_ENV)
        if env_max_concurrent:
            try:
                max_concurrent = int(env_max_concurrent)
            except ValueError:
                max_concurrent = deepeval_config.get(
                    "max_concurrent", DEFAULT_MAX_CONCURRENT
                )
        else:
            max_concurrent = deepeval_config.get(
                "max_concurrent", DEFAULT_MAX_CONCURRENT
            )

    return {
        "enabled": enabled,
        "judge_model": judge_model,
        "judge_model_provider": provider,
        "temperature": temperature,
        "max_concurrent": max_concurrent,
    }
