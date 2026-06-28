"""
LLM-powered answer generation for RAG.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the LLMGenerator class
which runs AWS Bedrock (Anthropic, Amazon, Meta) models. Bedrock-only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Final

# Load .env file if it exists
_env_path = Path.cwd() / ".env"
if _env_path.exists():
    from dotenv import load_dotenv

    load_dotenv(_env_path)

# Lazy tracer import to avoid circular dependency
_tracer = None

# ====================================================================
# GENERATOR PROVIDER / MODEL DEFAULTS (Bedrock-only)
# ====================================================================
# Bedrock is the default provider. The default model is an AU-geographic
# inference profile (au.*) so Australian legal/PII data stays in-country.
# Use au.* NOT apac.* (apac routes across the broad APAC region, a
# residency regression). Confirm the exact dated au.* string and account
# model-access against AWS "Supported Regions and models for inference
# profiles" for ap-southeast-2 before merging; these change.
DEFAULT_GENERATOR_PROVIDER: Final[str] = "bedrock"
DEFAULT_GENERATOR_MODEL: Final[str] = "au.anthropic.claude-sonnet-4-6"

# Geographic inference-profile prefixes (cross-region routing). Recognising
# these keeps au.anthropic... from being misclassified as OpenAI by the
# dev-only prefix sniff in _is_bedrock_model.
_BEDROCK_GEO_PREFIXES: Final[tuple[str, ...]] = (
    "us.",
    "eu.",
    "apac.",
    "au.",
    "global.",
)
# Bare model-family prefixes used by Bedrock foundation-model IDs.
_BEDROCK_FAMILY_PREFIXES: Final[tuple[str, ...]] = (
    "anthropic.",
    "amazon.",
    "meta.",
    "mistral.",
    "cohere.",
)

# ====================================================================
# BEDROCK THROTTLING RETRY / BACKOFF
# ====================================================================
# Throttling (ThrottlingException) is a transient condition, NOT a
# failure-to-zero: it must be retried with exponential backoff rather
# than surfaced as a hard error. Non-throttle ClientErrors (AccessDenied,
# ValidationException, ...) are NOT retried and are re-raised with their
# original error code preserved so callers and the preflight can inspect
# it. _sleep is module-level so tests can patch it and avoid real sleeping.
_BEDROCK_MAX_RETRIES: Final[int] = 5
_BEDROCK_BASE_BACKOFF_SECONDS: Final[float] = 0.5
_BEDROCK_MAX_BACKOFF_SECONDS: Final[float] = 16.0
_THROTTLING_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "Throttling"}
)


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep so the backoff is patchable in tests."""
    time.sleep(seconds)


def _bedrock_backoff_seconds(attempt: int) -> float:
    """Capped exponential backoff (seconds) for the given 0-based attempt."""
    return min(
        _BEDROCK_BASE_BACKOFF_SECONDS * (2**attempt),
        _BEDROCK_MAX_BACKOFF_SECONDS,
    )


def _client_error_code(error: Any) -> str | None:
    """Extract the AWS error code from a botocore ClientError, if present."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def _get_tracer():
    """Get global tracer instance for span emission."""
    global _tracer
    if _tracer is None:
        try:
            from opentelemetry.trace import get_tracer

            # Use globally registered tracer via set_global_tracer_provider=True
            _tracer = get_tracer(__name__)
        except (ImportError, Exception):
            pass  # Tracing not available
    return _tracer


def _is_bedrock_model(model: str) -> bool:
    """
    Check if a model identifier is for AWS Bedrock.

    DEV-ONLY convenience: an explicit CRUCIBLE_GENERATOR_PROVIDER is the
    correctness path. This sniff recognises both bare family prefixes
    (anthropic., amazon., ...) AND geographic inference-profile prefixes
    (us., eu., apac., au., global.) so that profile IDs like
    au.anthropic.claude-sonnet-4-6 are not misclassified as OpenAI.
    """
    return model.startswith(_BEDROCK_GEO_PREFIXES + _BEDROCK_FAMILY_PREFIXES)


def _resolve_generator_provider_and_model(
    model: str | None = None,
) -> tuple[str, str]:
    """
    Resolve the generator (provider, model) from the env-var contract.

    Contract (env > explicit-call > default; no YAML wiring at the generator):
    - CRUCIBLE_GENERATOR_PROVIDER: "bedrock" (the only supported provider).
    - CRUCIBLE_GENERATOR_MODEL: model ID (inference-profile ID for bedrock).
    - Explicit provider WINS; FAIL LOUD on provider/model disagreement.
    - Fail-loud rename: if the old RAG_GENERATOR_* is set while the matching
      CRUCIBLE_GENERATOR_* is unset, raise — never silently alias or fall back.

    Args:
        model: Explicit model override (e.g. passed to LLMGenerator). When
            provided it takes precedence over CRUCIBLE_GENERATOR_MODEL.

    Returns:
        (provider, model) tuple.

    Raises:
        ValueError: On the fail-loud rename, or on provider/model disagreement.

    """
    # Fail-loud rename: never silently alias the old var or fall back.
    if os.getenv("RAG_GENERATOR_MODEL") is not None and (
        os.getenv("CRUCIBLE_GENERATOR_MODEL") is None
    ):
        raise ValueError(
            "RAG_GENERATOR_MODEL is renamed to CRUCIBLE_GENERATOR_MODEL; update your config."
        )

    # Resolve model: explicit call arg > env > default.
    if model is None:
        model = os.getenv("CRUCIBLE_GENERATOR_MODEL", DEFAULT_GENERATOR_MODEL)

    # Resolve provider: explicit env > default. This project is BEDROCK-ONLY
    # (OpenAI/gpt-4o removed), so the only valid provider is "bedrock".
    explicit_provider = os.getenv("CRUCIBLE_GENERATOR_PROVIDER")
    if explicit_provider is not None:
        provider = explicit_provider.strip().lower()
        if provider != "bedrock":
            raise ValueError(
                f"Unsupported CRUCIBLE_GENERATOR_PROVIDER: {explicit_provider!r}. "
                "This project is Bedrock-only (OpenAI/gpt-4o removed); use 'bedrock'."
            )

    # FAIL LOUD if the model id does not look like a Bedrock inference profile.
    if not _is_bedrock_model(model):
        raise ValueError(
            f"CRUCIBLE_GENERATOR_MODEL={model!r} does not look like a Bedrock "
            "model/inference-profile ID. This project is Bedrock-only; fix the model ID."
        )
    return "bedrock", model


def _resolve_region() -> str:
    """
    Resolve the AWS region for Bedrock, env > YAML, one source of truth.

    boto3 reads AWS_REGION / AWS_DEFAULT_REGION natively; we do NOT
    reimplement resolution, but we read the same vars to determine the
    region_name value to pass explicitly to the client and to fail loud
    when it is unset (no implicit us-east-1 fallback — a residency hazard).

    Returns:
        The resolved region name.

    Raises:
        ValueError: If neither AWS_REGION nor AWS_DEFAULT_REGION is set.

    """
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if not region:
        raise ValueError(
            "AWS region is not set for a Bedrock run. Set AWS_REGION (or "
            "AWS_DEFAULT_REGION) to a region matching your inference-profile "
            "geography (e.g. ap-southeast-2 for au.* profiles). Refusing to "
            "default to us-east-1 (data-residency hazard)."
        )
    return region


class LLMGenerator:
    """
    LLM-powered answer generator for RAG.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    Supports:
    All models run on AWS Bedrock (Bedrock-only project).
    - AWS Bedrock (default): au.anthropic.claude-sonnet-4-6 and other
      inference-profile / foundation-model IDs.

    Attributes:
        _model: Model identifier.
        _provider: "openai" or "bedrock".
        _api_key: API key (for OpenAI).
        _deterministic_mode: Whether to use deterministic generation (temp=0).

    Example:
        >>> generator = LLMGenerator()  # Uses CRUCIBLE_GENERATOR_* env vars
        >>> answer = generator.generate(
        ...     question="What is this?",
        ...     retrieved_chunks=[...]
        ... )
        >>> print(answer["text"])

    """

    __slots__ = ("_model", "_provider", "_api_key", "_deterministic_mode")

    MODEL_NAME_ATTR: Final[str] = "llm.model_name"
    INPUT_MESSAGE_ATTR: Final[str] = "llm.input_messages.{i}.message"
    OUTPUT_MESSAGE_ATTR: Final[str] = "llm.output_messages.{i}.message"
    TOKEN_COUNT_ATTR: Final[str] = "llm.token_count.total"
    MESSAGE_ROLE: Final[str] = "role"
    MESSAGE_CONTENT: Final[str] = "content"

    def __init__(self, model: str | None = None, deterministic_mode: bool = False) -> None:
        """
        Initialize LLM generator.

        Args:
            model: Model identifier. If None, resolves from the CRUCIBLE_GENERATOR_*
                env-var contract (defaulting to Bedrock + au.anthropic.claude-sonnet-4-6).
                Bedrock models start with a geographic profile prefix (au., us., ...)
                or a family prefix (anthropic., amazon., ...).
            deterministic_mode: If True, use temperature=0 for reproducible output.

        Raises:
            ValueError: If required credentials are missing, on the fail-loud
                rename of RAG_GENERATOR_*, or on provider/model disagreement.

        """
        provider, model = _resolve_generator_provider_and_model(model)
        self._model: str = model
        self._provider: str = provider  # always "bedrock" (Bedrock-only project)
        self._deterministic_mode: bool = deterministic_mode
        # Bedrock uses AWS credentials from the environment / AWS config chain.
        self._api_key = None

    def generate(self, question: str, retrieved_chunks: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Generate answer using LLM with retrieved context.

        Args:
            question: User question to answer.
            retrieved_chunks: List of retrieved chunks with chunk_id, text, etc.

        Returns:
            Dictionary containing:
                - text: Generated answer text
                - answer_supported: Whether corpus supports the answer
                - citations: List of citation dictionaries
                - timings_ms: Timing information

        Raises:
            ValueError: If API call fails or returns invalid response.

        """
        tracer = _get_tracer()

        # Check if tracer is usable (not NoOpTracer)
        # NoOpTracer doesn't support OpenInference span kinds
        is_noop_tracer = (
            tracer is not None and hasattr(tracer, "real_tracer") and tracer.real_tracer is None
        )

        if tracer is None or is_noop_tracer:
            return self._generate_bedrock(question, retrieved_chunks)

        try:
            from openinference.semconv.trace import OpenInferenceSpanKindValues
        except ImportError:
            # Tracing deps (openinference) not installed and not needed when
            # Phoenix tracing is off — generate without emitting spans.
            return self._generate_bedrock(question, retrieved_chunks)

        LLM = OpenInferenceSpanKindValues.LLM

        # Try using tracer, fall back if it fails
        try:
            span_name = "bedrock.generate"
            span_context = tracer.start_as_current_span(
                span_name,
                openinference_span_kind=LLM,
            )
            span = span_context.__enter__()

            # Set model name
            span.set_attribute(self.MODEL_NAME_ATTR, self._model)

            # Set input message
            span.set_attribute(f"{self.INPUT_MESSAGE_ATTR}.{self.MESSAGE_ROLE}", "user")
            # Build context for prompt
            context_parts = []
            for chunk in retrieved_chunks:
                chunk_id = chunk.get("chunk_id", "unknown")
                text = chunk.get("text", "")
                context_parts.append(f"[{chunk_id}]: {text}")
            context = "\n\n".join(context_parts)
            user_message = f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
            span.set_attribute(f"{self.INPUT_MESSAGE_ATTR}.{self.MESSAGE_CONTENT}", user_message)

            # Generate answer
            result = self._generate_bedrock(question, retrieved_chunks)

            # Set output message attributes
            span.set_attribute(f"{self.OUTPUT_MESSAGE_ATTR}.{self.MESSAGE_ROLE}", "assistant")
            span.set_attribute(
                f"{self.OUTPUT_MESSAGE_ATTR}.{self.MESSAGE_CONTENT}",
                result.get("text", ""),
            )

            # Set token count (0 since we don't track it)
            span.set_attribute(self.TOKEN_COUNT_ATTR, 0)

            span_context.__exit__(None, None, None)
            return result

        except TypeError:
            # Tracer doesn't support OpenInference params (NoOpTracer)
            # Fall back to direct generation
            return self._generate_bedrock(question, retrieved_chunks)

    def _generate_bedrock(
        self, question: str, retrieved_chunks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Generate using AWS Bedrock API.

        The boto3 client is constructed with the resolved region and NO
        explicit credentials so the standard AWS credential chain engages.

        Error handling distinguishes failure modes:
        - ThrottlingException is transient: retried with capped exponential
          backoff, never surfaced as a zero/low score.
        - Any other botocore ClientError (AccessDenied, ValidationException,
          ...) is re-raised UNCHANGED so its error code stays inspectable by
          callers and the startup preflight; it is NOT flattened into a
          generic ValueError or retried.

        Raises:
            botocore.exceptions.ClientError: For non-throttle AWS errors, and
                for throttling that persists past the bounded retry budget
                (error code preserved in both cases).

        """
        start_time = time.perf_counter()

        context_parts = []
        for chunk in retrieved_chunks:
            chunk_id = chunk.get("chunk_id", "unknown")
            text = chunk.get("text", "")
            context_parts.append(f"[{chunk_id}]: {text}")

        context = "\n\n".join(context_parts)

        system_prompt = (
            "You are a helpful assistant that answers questions based on "
            "the provided context.\n"
            "When answering, you MUST cite your sources using the chunk_ids "
            "in square brackets like [chunk_id].\n"
            'For example: "The answer is [doc1_chunk_00000]."\n\n'
            "If the context doesn't contain enough information to answer "
            "the question confidently, say \"I don't have enough information "
            'to answer this question."\n'
        )

        user_message = f"""Context:
{context}

Question: {question}

Answer:"""

        import boto3

        region_name = _resolve_region()
        client = boto3.client("bedrock-runtime", region_name=region_name)

        # Anthropic Claude format (bare family or geographic profile ID)
        if "anthropic." in self._model:
            request_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "temperature": 0.0 if self._deterministic_mode else 0.0,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }
        # Amazon Titan / Meta Llama / other models
        else:
            request_body = {
                "maxTokenCount": 1024,
                "temperature": 0.0 if self._deterministic_mode else 0.0,
                "textGenerationConfig": {
                    "maxTokenCount": 1024,
                    "temperature": 0.0 if self._deterministic_mode else 0.0,
                },
                "inputText": f"{system_prompt}\n\n{user_message}",
            }

        response = self._invoke_bedrock_with_retry(client, request_body)

        response_body = json.loads(response["body"].read())

        # Parse response based on model type
        if "anthropic." in self._model:
            answer_text = response_body.get("content", [{}])[0].get("text", "")
        else:
            answer_text = response_body.get("results", [{}])[0].get("outputText", "")

        answer_supported = "I don't have enough information" not in answer_text
        generation_time = (time.perf_counter() - start_time) * 1000

        return {
            "text": answer_text,
            "answer_supported": answer_supported,
            "citations": [],
            "timings_ms": {"generation": generation_time},
        }

    def _invoke_bedrock_with_retry(self, client: Any, request_body: dict[str, Any]) -> Any:
        """
        Invoke the Bedrock model, retrying ONLY on throttling.

        ThrottlingException is retried with capped exponential backoff up to
        _BEDROCK_MAX_RETRIES. Every other botocore ClientError is re-raised
        immediately with its error code preserved (no flattening to
        ValueError, no retry). If throttling exhausts the retry budget the
        ThrottlingException is re-raised with its code intact.
        """
        from botocore.exceptions import ClientError

        last_throttle: ClientError | None = None
        for attempt in range(_BEDROCK_MAX_RETRIES + 1):
            try:
                return client.invoke_model(
                    modelId=self._model,
                    body=json.dumps(request_body),
                )
            except ClientError as error:
                # Preserve the error code; only throttling is retryable.
                if _client_error_code(error) in _THROTTLING_ERROR_CODES:
                    last_throttle = error
                    if attempt < _BEDROCK_MAX_RETRIES:
                        _sleep(_bedrock_backoff_seconds(attempt))
                        continue
                # Non-throttle ClientError, or throttling past the budget:
                # re-raise UNCHANGED so the code stays inspectable.
                raise
        # Unreachable in practice; satisfies type-checkers.
        raise last_throttle  # type: ignore[misc]


# Aliases for backward compatibility with imports
ClaudeGenerator = LLMGenerator
