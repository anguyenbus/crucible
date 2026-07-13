"""
Bedrock runtime client: Titan V2 query embedding + Claude answer generation.

Constructed ONCE in FastAPI lifespan and injected into the pipeline by the
router — pipeline stages receive the instance and never import boto3
themselves (``stages-pure`` import-linter contract). Credentials come from
the ambient boto3 credential chain; timeouts are boto defaults.

Behavior pins (model ids, temperature, ``max_tokens``) arrive PER CALL from
the resolved pinned config — this module holds no behavior of its own beyond
the throttling retry policy.

Retry policy (mirrors eval's ``generator.py`` policy — pattern, never an
import): ONLY throttling is retried, with capped exponential backoff through
the module-level patchable ``_sleep``; exhaustion raises
:class:`BedrockThrottleExhaustedError` (the 503 seam). Every other botocore
``ClientError`` is re-raised UNCHANGED (the 502 seam) so its error code stays
inspectable.

Results are PLAIN DATA (text, token counts, model attrs) — this module never
imports OpenTelemetry; the router attaches span attributes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from app.clients.errors import BedrockThrottleExhaustedError

# Titan V2 query-embedding shape pinned by the BYO index contract: 1024-dim,
# normalized, cosinesimil (docs/byo-index-contract.md).
TITAN_EMBEDDING_DIMENSIONS: Final[int] = 1024

_MAX_RETRIES: Final[int] = 5
_BASE_BACKOFF_SECONDS: Final[float] = 0.5
_MAX_BACKOFF_SECONDS: Final[float] = 16.0
_THROTTLING_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "Throttling"}
)


def _sleep(seconds: float) -> None:
    """Indirection over time.sleep so the backoff is patchable in tests."""
    time.sleep(seconds)


def _backoff_seconds(attempt: int) -> float:
    """Capped exponential backoff (seconds) for the given 0-based attempt."""
    return min(_BASE_BACKOFF_SECONDS * (2**attempt), _MAX_BACKOFF_SECONDS)


def _client_error_code(error: ClientError) -> str | None:
    """Extract the AWS error code from a botocore ClientError, if present."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


@dataclass(frozen=True)
class GenerationResult:
    """
    One generation outcome as PLAIN DATA (no telemetry types).

    Token counts and model attrs exist so the router can attach them as span
    attributes; ``None`` means Bedrock did not report the value.
    """

    text: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    stop_reason: str | None


class GenerationStream:
    """
    One STREAMING generation: iterate text deltas, then read the plain result.

    Wraps the raw ``invoke_model_with_response_stream`` event stream
    (Anthropic messages dialect). Iterating yields ``content_block_delta``
    text deltas AS RECEIVED; token counts and stop reason are accumulated
    from the ``message_start`` / ``message_delta`` / ``message_stop``
    (invocation-metrics) events and exposed via :meth:`result` AFTER the
    stream is fully consumed — plain data, no telemetry types.

    A failure raised while iterating (e.g. a mid-stream throttle or
    ``ClientError``) propagates to the caller UNCHANGED and is NEVER retried
    here — retrying would replay partial text. It is the router's mid-stream
    ``error``-event seam.
    """

    def __init__(self, event_stream: Any, *, model_id: str) -> None:
        """
        Wrap one raw Bedrock response event stream.

        Args:
            event_stream: The ``response["body"]`` event stream from
                ``invoke_model_with_response_stream``.
            model_id: The generator model id (echoed into the result).

        """
        self._events = event_stream
        self._model_id = model_id
        self._text_parts: list[str] = []
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._stop_reason: str | None = None
        self._finished = False

    def __iter__(self) -> Iterator[str]:
        """Yield text deltas as they arrive; record counts along the way."""
        for event in self._events:
            chunk = event.get("chunk")
            if not chunk:
                continue
            payload = json.loads(chunk["bytes"])
            kind = payload.get("type")
            if kind == "message_start":
                usage = payload.get("message", {}).get("usage", {})
                self._input_tokens = usage.get("input_tokens", self._input_tokens)
            elif kind == "content_block_delta":
                text = payload.get("delta", {}).get("text")
                if text:
                    self._text_parts.append(text)
                    yield text
            elif kind == "message_delta":
                self._stop_reason = payload.get("delta", {}).get("stop_reason", self._stop_reason)
                usage = payload.get("usage", {})
                self._output_tokens = usage.get("output_tokens", self._output_tokens)
            elif kind == "message_stop":
                metrics = payload.get("amazon-bedrock-invocationMetrics", {})
                self._input_tokens = metrics.get("inputTokenCount", self._input_tokens)
                self._output_tokens = metrics.get("outputTokenCount", self._output_tokens)
        self._finished = True

    def result(self) -> GenerationResult:
        """
        Return the post-stream outcome as PLAIN DATA (mirrors ``generate()``).

        Returns:
            GenerationResult over the FULL accumulated text plus token
            counts/stop reason from the stream's metric events.

        Raises:
            RuntimeError: If the stream has not been fully consumed yet —
                token counts only exist after ``message_stop``.

        """
        if not self._finished:
            raise RuntimeError(
                "GenerationStream.result() requires the stream to be fully consumed."
            )
        return GenerationResult(
            text="".join(self._text_parts),
            model_id=self._model_id,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            stop_reason=self._stop_reason,
        )


class BedrockClient:
    """
    Thin bedrock-runtime wrapper: ``embed_query``, ``generate``, ``generate_stream``.

    Attributes:
        _runtime: boto3 ``bedrock-runtime`` client (injectable for tests).

    """

    def __init__(self, *, region: str, runtime_client: Any | None = None) -> None:
        """
        Create the client against one region.

        Args:
            region: AWS region for bedrock-runtime (pinned config carries it).
            runtime_client: Optional pre-built client (tests inject a fake);
                default builds a boto3 client on the ambient credential chain.

        """
        self._runtime = (
            runtime_client
            if runtime_client is not None
            else boto3.client("bedrock-runtime", region_name=region)
        )

    def embed_query(
        self,
        text: str,
        *,
        model_id: str,
        dimensions: int = TITAN_EMBEDDING_DIMENSIONS,
        normalize: bool = True,
    ) -> list[float]:
        """
        Embed ONE query text with Titan V2 (1024-dim, normalized).

        Args:
            text: The query text to embed.
            model_id: Bedrock embedding model id (config pin).
            dimensions: Requested output dimension (index-contract 1024).
            normalize: Request unit-normalized vectors from Titan.

        Returns:
            The embedding vector as plain floats.

        Raises:
            BedrockThrottleExhaustedError: Throttling outlasted the retries.
            ClientError: Any non-throttle AWS error, unchanged.
            ValueError: If Titan returns an unexpected dimension.

        """
        body = {"inputText": text, "dimensions": dimensions, "normalize": normalize}
        response = self._invoke_with_retry(model_id, body)
        payload = json.loads(response["body"].read())
        embedding = payload["embedding"]
        if len(embedding) != dimensions:
            raise ValueError(
                f"Titan returned a {len(embedding)}-dim vector; expected "
                f"{dimensions} (model {model_id})"
            )
        return [float(value) for value in embedding]

    def generate(
        self,
        prompt: str,
        *,
        model_id: str,
        temperature: float,
        max_tokens: int,
    ) -> GenerationResult:
        """
        Generate an answer with a Claude model (Anthropic messages format).

        Args:
            prompt: The fully built prompt (template already rendered).
            model_id: Bedrock generator model/inference-profile id (config pin).
            temperature: Sampling temperature (config pin).
            max_tokens: Generation cap (config pin).

        Returns:
            GenerationResult with answer text plus token counts/model attrs
            as plain data for span attribution by the router.

        Raises:
            BedrockThrottleExhaustedError: Throttling outlasted the retries.
            ClientError: Any non-throttle AWS error, unchanged.

        """
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = self._invoke_with_retry(model_id, body)
        payload = json.loads(response["body"].read())
        usage = payload.get("usage", {})
        return GenerationResult(
            text=payload.get("content", [{}])[0].get("text", ""),
            model_id=model_id,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            stop_reason=payload.get("stop_reason"),
        )

    def generate_stream(
        self,
        prompt: str,
        *,
        model_id: str,
        temperature: float,
        max_tokens: int,
    ) -> GenerationStream:
        """
        Stream an answer with a Claude model (Anthropic messages format).

        Sends the SAME Anthropic messages body as :meth:`generate` via
        ``invoke_model_with_response_stream`` (the drop-in streaming
        counterpart; ``converse_stream`` is rejected — a second body dialect
        for no gain). Throttling retry applies to the INITIAL call only,
        sharing :meth:`generate`'s exact policy; failures raised while
        ITERATING the returned stream propagate unchanged and are never
        retried (see :class:`GenerationStream`).

        Args:
            prompt: The fully built prompt (template already rendered).
            model_id: Bedrock generator model/inference-profile id (config pin).
            temperature: Sampling temperature (config pin).
            max_tokens: Generation cap (config pin).

        Returns:
            GenerationStream yielding text deltas, with post-stream token
            counts/stop reason available via ``result()``.

        Raises:
            BedrockThrottleExhaustedError: Throttling outlasted the retries
                on the initial call.
            ClientError: Any non-throttle AWS error on the initial call,
                unchanged.

        """
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = self._call_with_throttle_retry(
            model_id,
            lambda: self._runtime.invoke_model_with_response_stream(
                modelId=model_id,
                body=json.dumps(body),
            ),
        )
        return GenerationStream(response["body"], model_id=model_id)

    def credentials_resolve(self) -> bool:
        """
        Report whether the ambient boto3 credential chain resolves credentials.

        This is the readyz Bedrock probe: it verifies the CREDENTIAL CHAIN
        only (env/profile/IMDS), deliberately NOT invoke permission — no paid
        probe is ever made. Invoke permission is proven by the first real
        generation (the acceptance run).
        """
        return boto3.Session().get_credentials() is not None

    def _invoke_with_retry(self, model_id: str, request_body: dict[str, Any]) -> Any:
        """
        Invoke the model, retrying ONLY on throttling (bounded, capped backoff).

        Raises:
            BedrockThrottleExhaustedError: Throttling persisted past the
                retry budget (the last ClientError is chained as the cause).
            ClientError: Any non-throttle AWS error, re-raised unchanged so
                its error code stays inspectable.

        """
        return self._call_with_throttle_retry(
            model_id,
            lambda: self._runtime.invoke_model(
                modelId=model_id,
                body=json.dumps(request_body),
            ),
        )

    def _call_with_throttle_retry(self, model_id: str, invoke: Callable[[], Any]) -> Any:
        """
        Run ``invoke`` under the ONE retry policy both paths share.

        Retries ``invoke`` ONLY on throttling with bounded, capped exponential
        backoff through the patchable module-level ``_sleep``. The streaming
        path passes its INITIAL ``invoke_model_with_response_stream`` call —
        mid-stream failures never reach this loop by construction.

        Raises:
            BedrockThrottleExhaustedError: Throttling persisted past the
                retry budget (the last ClientError is chained as the cause).
            ClientError: Any non-throttle AWS error, re-raised unchanged so
                its error code stays inspectable.

        """
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return invoke()
            except ClientError as error:
                if _client_error_code(error) not in _THROTTLING_ERROR_CODES:
                    raise
                if attempt >= _MAX_RETRIES:
                    raise BedrockThrottleExhaustedError(
                        f"Bedrock throttling persisted after {_MAX_RETRIES} "
                        f"retries for model {model_id}."
                    ) from error
                _sleep(_backoff_seconds(attempt))
        raise AssertionError("unreachable")  # pragma: no cover
