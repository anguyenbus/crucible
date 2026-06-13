"""
Phoenix adapter for RAG pipeline observability.

Uses phoenix.otel.register() to configure OpenTelemetry OTLP export via gRPC.

KEY INSIGHT: OpenTelemetry parent-child relationships are established through
"current span" context. When you use start_as_current_span() within another
span's context, it automatically becomes a child.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from beartype import beartype

if TYPE_CHECKING:
    pass

from openinference.semconv.trace import OpenInferenceSpanKindValues

# OpenInference span kind values
CHAIN = OpenInferenceSpanKindValues.CHAIN
RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER
LLM = OpenInferenceSpanKindValues.LLM
EVALUATOR = OpenInferenceSpanKindValues.EVALUATOR

# Constants
DEFAULT_ENDPOINT: Final[str] = "http://localhost:6006"
DEFAULT_PROJECT_NAME: Final[str] = "crucible"
PHOENIX_ENDPOINT_ENV: Final[str] = "PHOENIX_ENDPOINT"


@beartype
class PhoenixAdapter:
    """
    Phoenix adapter for RAG pipeline observability.

    Creates span hierarchy: eval_run (root) -> rag_query -> retrieval,
    generation, evaluation.

    IMPORTANT: For proper parent-child relationships, child spans must be created
    while the parent span is the "current" span in OpenTelemetry context.

    Example usage with evaluation run grouping:
        >>> with adapter.eval_run_span("legal-rag-bench-nano", num_questions=10):
        ...     for query in dataset:
        ...         with adapter.rag_query_span(query) as trace_id:
        ...             adapter.retrieval_span(trace_id, ...)
        ...             adapter.evaluation_span(trace_id, ...)

    NOTE: LLM judge internal calls (DeepEval metrics) are NOT traced - only
    the final evaluation span with scores and reasoning. This keeps traces
    focused on user-visible behavior rather than implementation details.

    """

    __slots__ = (
        "_endpoint",
        "_project_name",
        "_enabled",
        "_export_path",
        "_tracer_provider",
        "_tracer",
        "_evaluations",
        "_active_root_span",
        "_active_eval_run",
        "_current_session_id",
    )

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        project_name: str = DEFAULT_PROJECT_NAME,
        enabled: bool = True,
        export_path: Path | None = None,
    ) -> None:
        """
        Initialize Phoenix adapter.

        Args:
            endpoint: Phoenix UI endpoint (e.g., http://localhost:6006).
                Internally converted to gRPC endpoint (http://localhost:4317).
            project_name: Phoenix project name for grouping traces.
            enabled: Whether to enable Phoenix tracing.
            export_path: Fallback path for Parquet export when Phoenix unavailable.

        """
        self._endpoint: str = self._validate_endpoint(endpoint)
        self._project_name: str = project_name
        self._enabled: bool = enabled
        self._export_path: Path | None = export_path
        self._tracer_provider: Any = None
        self._tracer: Any = None
        self._evaluations: list[dict[str, Any]] = []
        self._active_root_span: Any = None
        self._active_eval_run: Any = None
        self._current_session_id: str | None = None

        if enabled:
            self._initialize()

    def _validate_endpoint(self, endpoint: str) -> str:
        """Validate Phoenix endpoint URL format."""
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(
                f"Invalid Phoenix endpoint URL: {endpoint}. "
                "Endpoint must start with http:// or https://"
            )
        return endpoint

    def _get_otlp_endpoint(self, ui_endpoint: str) -> str:
        """
        Convert UI endpoint to gRPC endpoint (Phoenix's recommended default).

        Phoenix accepts OTLP via:
        - HTTP at http://localhost:6006/v1/traces (UI port + path)
        - gRPC at http://localhost:4317 (separate gRPC listener)

        We use gRPC (4317) as it's Phoenix's default and more efficient.
        """
        import re

        # Replace UI port (6006) with gRPC port (4317)
        match = re.match(r"(https?://[^:]+):\d+", ui_endpoint)
        if match:
            return f"{match.group(1)}:4317"
        return "http://localhost:4317"

    def _initialize(self) -> None:
        """Initialize OpenTelemetry tracer using Phoenix register()."""
        try:
            from phoenix.otel import register

            # Use gRPC endpoint (Phoenix's recommended default)
            otlp_endpoint = self._get_otlp_endpoint(self._endpoint)

            # Register with Phoenix: batch processing, no global registration, quiet
            self._tracer_provider = register(
                endpoint=otlp_endpoint,
                project_name=self._project_name,
                protocol="grpc",
                batch=True,  # Use BatchSpanProcessor (production-ready)
                set_global_tracer_provider=False,  # Don't set global default
                verbose=False,  # Suppress verbose output
            )

            self._tracer = self._tracer_provider.get_tracer("crucible")

        except Exception as e:
            import sys

            print(
                f"[WARN] Phoenix initialization failed: {e}. Traces will be buffered to Parquet.",
                file=sys.stderr,
            )
            self._tracer = None

    @beartype
    def is_connected(self) -> bool:
        """Check if Phoenix tracer is available."""
        return self._tracer is not None

    @contextmanager
    def eval_run_span(
        self,
        run_name: str,
        num_questions: int = 0,
        metadata: dict[str, Any] | None = None,
    ):
        """
        Context manager for evaluation run span (groups all queries).

        This creates a parent span that groups all RAG query traces.
        In Phoenix UI, you'll see one "eval_run" trace with all queries as children.

        Usage:
            with adapter.eval_run_span("legal-rag-bench-nano", num_questions=10):
                for query in dataset:
                    with adapter.rag_query_span(query) as trace_id:
                        ...

        Args:
            run_name: Name of the evaluation run.
            num_questions: Number of questions in the dataset.
            metadata: Additional metadata (slice_name, top_k, etc.)

        Yields:
            run_id: Unique identifier for this evaluation run.

        """
        run_id = str(uuid.uuid4())

        if self._tracer:
            # Set session_id for grouping in Phoenix UI
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self._current_session_id = f"eval-{run_name}-{timestamp}"

            with self._tracer.start_as_current_span(
                name="eval_run", openinference_span_kind=CHAIN
            ) as span:
                span.set_attribute("eval.run_name", run_name)
                span.set_attribute("eval.num_questions", num_questions)
                span.set_attribute("eval.session_id", self._current_session_id)
                if metadata:
                    for key, value in metadata.items():
                        span.set_attribute(f"eval.{key}", str(value))
                self._active_eval_run = span
                try:
                    yield run_id
                finally:
                    self._active_eval_run = None
                    self._current_session_id = None
        else:
            yield run_id

    @contextmanager
    def rag_query_span(self, question: str):
        """
        Context manager for root RAG query span.

        This establishes the root span as the "current" span. Any child spans
        created within this context will automatically become children.

        If called within eval_run_span context, becomes a child of eval_run.

        Usage:
            with adapter.rag_query_span("What is contract law?") as trace_id:
                adapter.retrieval_span(trace_id, ...)
                adapter.generation_span(trace_id, ...)

        Yields:
            trace_id: Unique identifier for this trace.

        """
        trace_id = str(uuid.uuid4())

        if self._tracer:
            from openinference.instrumentation import using_attributes

            # Add session_id from parent eval_run if available
            if self._current_session_id:
                with using_attributes(session_id=self._current_session_id):
                    with self._tracer.start_as_current_span(
                        name="rag_query", openinference_span_kind=CHAIN
                    ) as span:
                        span.set_attribute("question", question)
                        span.set_attribute("input", question)
                        self._active_root_span = span
                        try:
                            yield trace_id
                        finally:
                            self._active_root_span = None
            else:
                with self._tracer.start_as_current_span(
                    name="rag_query", openinference_span_kind=CHAIN
                ) as span:
                    span.set_attribute("question", question)
                    span.set_attribute("input", question)
                    self._active_root_span = span
                    try:
                        yield trace_id
                    finally:
                        self._active_root_span = None
        else:
            yield trace_id

    @beartype
    def start_retrieval_span(
        self,
        trace_id: str,
        query_text: str,
        chunks: list[dict[str, Any]],
        k: int,
        timing_ms: float = 0.0,
    ) -> None:
        """
        Record a retrieval span (non-context-manager version).

        Args:
            trace_id: Unique trace identifier.
            query_text: Original query text.
            chunks: Retrieved chunk documents.
            k: Number of chunks retrieved.
            timing_ms: Retrieval latency in milliseconds.

        """
        if self._tracer and self._active_root_span:
            with self._tracer.start_as_current_span(
                name="retrieval", openinference_span_kind=RETRIEVER
            ) as span:
                span.set_attribute("query", query_text)
                span.set_attribute("retrieval.k", k)
                span.set_attribute("retrieval.timing_ms", timing_ms)
                span.set_attribute("output", str(chunks))

    @beartype
    def start_generation_span(
        self,
        trace_id: str,
        model: str,
        prompt: str,
        tokens: int = 0,
        timing_ms: float = 0.0,
    ) -> None:
        """
        Record a generation span (non-context-manager version).

        Args:
            trace_id: Unique trace identifier.
            model: Model name used for generation.
            prompt: Generation prompt.
            tokens: Number of tokens generated.
            timing_ms: Generation latency in milliseconds.

        """
        if self._tracer and self._active_root_span:
            with self._tracer.start_as_current_span(
                name="generation", openinference_span_kind=LLM
            ) as span:
                span.set_attribute("model", model)
                span.set_attribute("input", prompt)
                span.set_attribute("output.tokens", tokens)
                span.set_attribute("llm.timing_ms", timing_ms)

    @beartype
    def start_evaluation_span(
        self,
        trace_id: str,
        scores: dict[str, Any],
        verdict: str = "PASS",
        reasoning: dict[str, Any] | None = None,
    ) -> None:
        """
        Record an evaluation span with metric scores.

        Args:
            trace_id: Unique trace identifier.
            scores: Dictionary of metric scores (faithfulness, etc.).
            verdict: Overall verdict (PASS/NEEDS_REVIEW/ERROR).
            reasoning: Optional reasoning dictionary per metric.

        """
        if self._tracer and self._active_root_span:
            with self._tracer.start_as_current_span(
                name="evaluation", openinference_span_kind=EVALUATOR
            ) as span:
                span.set_attribute("evaluation.verdict", verdict)
                for metric_name, score in scores.items():
                    span.set_attribute(f"evaluation.{metric_name}", str(score))

                if reasoning:
                    span.set_attribute("evaluation.reasoning", str(reasoning))


@contextmanager
def _noop_suppression() -> Any:
    """No-op tracing suppression used when Phoenix is unavailable."""
    yield


def suppress_tracing_if_available() -> Any:
    """
    Return a Phoenix ``suppress_tracing`` context manager if Phoenix is available.

    This impure (observability-layer) helper performs the Phoenix reach-in that
    the pure kernel forbids. It is injected into the kernel
    ``DeepEvalEvaluator(metrics=..., suppress_tracing=...)`` so LLM-judge calls do
    not pollute Phoenix traces with noisy child spans. Falls back to a no-op
    context manager when Phoenix is not installed.

    Returns:
        A context manager (Phoenix ``suppress_tracing`` or a no-op).

    """
    try:
        from phoenix.core.tracing import suppress_tracing

        return suppress_tracing()
    except (ImportError, AttributeError):
        return _noop_suppression()


def get_phoenix_config(
    config: dict[str, Any],
    cli_enabled: bool | None = None,
    cli_endpoint: str | None = None,
) -> dict[str, Any]:
    """
    Get Phoenix configuration from multiple sources with precedence.

    Precedence order (highest to lowest):
    1. CLI arguments (cli_* parameters)
    2. Environment variables (PHOENIX_ENDPOINT)
    3. YAML config (phoenix section from config dict)
    4. Defaults

    Args:
        config: Loaded configuration dictionary from eval_config.yaml.
        cli_enabled: CLI flag for enabling/disabling Phoenix.
        cli_endpoint: CLI-specified Phoenix endpoint.

    Returns:
        Dictionary with Phoenix configuration keys:
            - enabled: bool
            - endpoint: str
            - project_name: str
            - mode: str ("spans" or "native")
            - export_path: str

    """
    # Get phoenix section from YAML config
    phoenix_config = config.get("phoenix", {})

    # Resolve enabled flag (CLI > YAML > default)
    if cli_enabled is not None:
        enabled = cli_enabled
    else:
        enabled = phoenix_config.get("enabled", False)

    # Resolve endpoint (CLI > env var > YAML > default)
    if cli_endpoint is not None:
        endpoint = cli_endpoint
    else:
        # Check environment variable first
        env_endpoint = os.environ.get(PHOENIX_ENDPOINT_ENV)
        if env_endpoint:
            endpoint = env_endpoint
        else:
            endpoint = phoenix_config.get("endpoint", DEFAULT_ENDPOINT)

    # Resolve other config values
    project_name = phoenix_config.get("project_name", "crucible")
    mode = phoenix_config.get("mode", "spans")
    export_path = phoenix_config.get("export_path", "data/phoenix/spans.parquet")

    return {
        "enabled": enabled,
        "endpoint": endpoint,
        "project_name": project_name,
        "mode": mode,
        "export_path": export_path,
    }
