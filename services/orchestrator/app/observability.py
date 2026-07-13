"""
OTel tracer bootstrap + OpenInference span helpers (spans live OUTSIDE stages).

The tracer is configured ONCE at FastAPI lifespan from ``PHOENIX_ENDPOINT``
(a Settings location fact). Unset → a genuine ``NoOpTracer`` and the service
is FULLY functional: every helper below degrades to no-ops and ``/query``
returns a complete, schema-valid response (``result.trace`` is then OMITTED;
``result.timings_ms`` is measured independently of tracing and always
present).

Export path: Phoenix accepts OTLP over HTTP on its UI port at ``/v1/traces``
(the compose file's ``PHOENIX_ENDPOINT=http://phoenix:6006``; the 4317 gRPC
listener also exists but would need a second, differently-shaped endpoint
value). OTLP-HTTP against the single shared ``PHOENIX_ENDPOINT`` matches
eval's proven export path and keeps the dependency set minimal (no gRPC
stack).

Span discipline: spans are created HERE and in the router — never inside
``app.orchestrator`` stages or ``app.clients`` (the ``stages-pure``
import-linter contract is unchanged; clients return plain data such as token
counts and the router attaches them via these helpers). Attribute names
follow the OpenInference semantic conventions so Phoenix parses the spans as
CHAIN / EMBEDDING / RETRIEVER / LLM kinds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from openinference.semconv.resource import ResourceAttributes
from openinference.semconv.trace import (
    DocumentAttributes,
    OpenInferenceSpanKindValues,
    SpanAttributes,
)
from opentelemetry import context as otel_context
from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.trace import (
    NoOpTracer,
    Span,
    Status,
    StatusCode,
    Tracer,
    format_span_id,
    format_trace_id,
)

if TYPE_CHECKING:
    from app.clients.bedrock import GenerationResult

# Phoenix project the spans (and result.trace.phoenix_project) belong to.
PHOENIX_PROJECT = "orchestrator"

_TRACER_NAME = "orchestrator.query-pipeline"


def configure_tracer(phoenix_endpoint: str | None) -> tuple[Tracer, Any | None]:
    """
    Build the pipeline tracer (lifespan-time; never at import).

    Args:
        phoenix_endpoint: Phoenix UI endpoint (e.g. ``http://localhost:6006``)
            from Settings, or ``None`` when ``PHOENIX_ENDPOINT`` is unset.

    Returns:
        ``(tracer, provider)`` — a real OTLP-exporting tracer plus its
        provider (so lifespan can ``shutdown()`` it), or
        ``(NoOpTracer(), None)`` when Phoenix is not configured.

    """
    if not phoenix_endpoint:
        return NoOpTracer(), None

    # SDK imports stay inside the configured branch so the no-op path (and
    # module import) never touches exporter machinery.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": "orchestrator",
            ResourceAttributes.PROJECT_NAME: PHOENIX_PROJECT,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{phoenix_endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(_TRACER_NAME), provider


def resolve_tracer(state: Any) -> Tracer:
    """Return the app.state tracer, or a NoOpTracer when lifespan never set one."""
    return getattr(state, "tracer", None) or NoOpTracer()


def _kinded_attributes(
    kind: OpenInferenceSpanKindValues, attributes: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge the OpenInference span-kind marker into the given attributes."""
    attrs: dict[str, Any] = {SpanAttributes.OPENINFERENCE_SPAN_KIND: kind.value}
    if attributes:
        attrs.update(attributes)
    return attrs


@contextmanager
def _span(
    tracer: Tracer,
    name: str,
    kind: OpenInferenceSpanKindValues,
    attributes: dict[str, Any] | None = None,
    context: Context | None = None,
) -> Iterator[Span]:
    """One OpenInference-kinded span; a plain no-op under the NoOpTracer."""
    with tracer.start_as_current_span(
        name, context=context, attributes=_kinded_attributes(kind, attributes)
    ) as span:
        yield span


def extract_trace_context(headers: Any) -> Context:
    """
    Extract incoming W3C trace context (``traceparent``/``tracestate``).

    Uses the OTel propagation API over the request headers — NO FastAPI
    auto-instrumentation dependency. An absent or invalid ``traceparent``
    yields an empty context, so the root span starts fresh: exactly the
    pre-extraction behavior. ``tracestate`` passes through via the
    propagator default; no sampling logic is added here.
    """
    from opentelemetry.propagate import extract

    return extract(headers)


def root_query_span(
    tracer: Tracer,
    *,
    question: str,
    route: str = "POST /query",
    context: Context | None = None,
):
    """Root span for one query request (span name parameterized per route)."""
    return _span(
        tracer,
        route,
        OpenInferenceSpanKindValues.CHAIN,
        {SpanAttributes.INPUT_VALUE: question},
        context=context,
    )


def start_root_query_span(
    tracer: Tracer,
    *,
    question: str,
    route: str,
    context: Context | None = None,
) -> Span:
    """
    Start the STREAMING route's root span WITHOUT making it ambient-current.

    The stream generator outlives the route handler (and may resume on other
    worker threads), so ``start_as_current_span``'s contextvar attach/detach
    cannot bracket it safely. The caller parents pre-generation work via
    :func:`use_context` + :func:`span_context` and MUST end the span
    explicitly when the stream terminates.
    """
    return tracer.start_span(
        route,
        context=context,
        attributes=_kinded_attributes(
            OpenInferenceSpanKindValues.CHAIN, {SpanAttributes.INPUT_VALUE: question}
        ),
    )


def span_context(span: Span) -> Context:
    """Build a Context with ``span`` installed — the explicit-parenting handle."""
    return trace_api.set_span_in_context(span)


@contextmanager
def use_context(context: Context) -> Iterator[None]:
    """Make ``context`` ambient-current for the block (attach/detach balanced)."""
    token = otel_context.attach(context)
    try:
        yield
    finally:
        otel_context.detach(token)


def start_generation_span(tracer: Tracer, *, model_id: str, prompt: str, context: Context) -> Span:
    """
    Start the streaming LLM span with an EXPLICIT parent (never ambient).

    Wraps the ENTIRE stream consumption inside the response generator; the
    caller ends it explicitly (see :func:`start_root_query_span` for why).
    """
    return tracer.start_span(
        "generation",
        context=context,
        attributes=_kinded_attributes(
            OpenInferenceSpanKindValues.LLM,
            {
                SpanAttributes.LLM_MODEL_NAME: model_id,
                SpanAttributes.INPUT_VALUE: prompt,
            },
        ),
    )


def start_citation_build_span(tracer: Tracer, *, context: Context) -> Span:
    """Start the streaming citation-build span with an EXPLICIT parent."""
    return tracer.start_span(
        "citation_build",
        context=context,
        attributes=_kinded_attributes(OpenInferenceSpanKindValues.CHAIN, None),
    )


def record_span_error(span: Span, error: BaseException) -> None:
    """Record a failure on a span (exception event + ERROR status)."""
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, str(error)))


def embedding_span(tracer: Tracer, *, model_id: str):
    """Child span for the query-embedding stage (Titan V2 model id)."""
    return _span(
        tracer,
        "embedding",
        OpenInferenceSpanKindValues.EMBEDDING,
        {SpanAttributes.EMBEDDING_MODEL_NAME: model_id},
    )


def retrieval_span(tracer: Tracer, *, question: str, index: str, host: str | None):
    """
    Child RETRIEVER span; carries the resolved index name + endpoint host.

    The Q8 provenance rule: identical ``config_sha256`` against a different
    index must be attributable from the span alone.
    """
    attrs: dict[str, Any] = {
        SpanAttributes.INPUT_VALUE: question,
        "opensearch.index": index,
    }
    if host is not None:
        attrs["opensearch.host"] = host
    return _span(tracer, "retrieval", OpenInferenceSpanKindValues.RETRIEVER, attrs)


def context_assembly_span(tracer: Tracer, *, chunks_in: int):
    """Child span for context assembly (chunk count in; char count set later)."""
    return _span(
        tracer,
        "context_assembly",
        OpenInferenceSpanKindValues.CHAIN,
        {"context.chunks_in": chunks_in},
    )


def generation_span(tracer: Tracer, *, model_id: str, prompt: str):
    """Child LLM span (model id + rendered prompt as input)."""
    return _span(
        tracer,
        "generation",
        OpenInferenceSpanKindValues.LLM,
        {
            SpanAttributes.LLM_MODEL_NAME: model_id,
            SpanAttributes.INPUT_VALUE: prompt,
        },
    )


def citation_build_span(tracer: Tracer):
    """Child span for citation building (counts attached by the router)."""
    return _span(tracer, "citation_build", OpenInferenceSpanKindValues.CHAIN)


def set_retrieval_documents(span: Span, chunks: list[dict[str, Any]]) -> None:
    """Attach retrieved documents per OpenInference retrieval conventions."""
    for position, chunk in enumerate(chunks):
        prefix = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{position}"
        span.set_attribute(f"{prefix}.{DocumentAttributes.DOCUMENT_ID}", chunk["chunk_id"])
        span.set_attribute(f"{prefix}.{DocumentAttributes.DOCUMENT_CONTENT}", chunk["text"])
        span.set_attribute(f"{prefix}.{DocumentAttributes.DOCUMENT_SCORE}", float(chunk["score"]))


def set_generation_attributes(span: Span, generation: GenerationResult) -> None:
    """Attach output text + token counts (plain data from the Bedrock client)."""
    span.set_attribute(SpanAttributes.OUTPUT_VALUE, generation.text)
    if generation.input_tokens is not None:
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, generation.input_tokens)
    if generation.output_tokens is not None:
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, generation.output_tokens)


def trace_echo(span: Span) -> dict[str, str] | None:
    """
    Build the ``result.trace`` block; ``None`` under the no-op tracer.

    A NoOpTracer yields a NON-RECORDING span — the contract is to OMIT
    ``trace`` entirely then, never to emit null/zero ids. The explicit
    ``is_recording`` guard matters with traceparent extraction: a no-op
    tracer handed a VALID extracted caller context returns a non-recording
    span that CARRIES the caller's ids — echoing those would claim tracing
    that never happened.
    """
    context = span.get_span_context()
    if not span.is_recording() or not context.is_valid:
        return None
    return {
        "trace_id": format_trace_id(context.trace_id),
        "span_id": format_span_id(context.span_id),
        "phoenix_project": PHOENIX_PROJECT,
    }


def start_guardrail_input_span(
    tracer: Tracer, *, model_id: str, context: Context | None = None
) -> Span:
    """
    Start the input-guard LLM span for the Haiku classifier call (explicit start).

    Created by the router ONLY when the classifier actually runs (a pre-filter
    hit), so a benign pre-filter miss adds no span. An OpenInference LLM span so
    Phoenix renders the guard call with its model id, token counts, and latency —
    on a SAFE allow AND a block (previously the SAFE call was invisible and a
    block recorded only a zero-duration decision span). ``start_span`` (not
    ``start_as_current_span``) is used so a block — which raises through the
    caller — does NOT auto-mark the span as an ERROR: a block is an intentional
    refusal, and the caller ends the span cleanly. Parents to the current span
    (blocking route) or the explicit root ``context`` (streaming route).
    """
    return tracer.start_span(
        "guardrail_input",
        context=context,
        attributes=_kinded_attributes(
            OpenInferenceSpanKindValues.LLM,
            {SpanAttributes.LLM_MODEL_NAME: model_id, "guardrail.stage": "input"},
        ),
    )


def set_guardrail_input_attributes(
    span: Span,
    *,
    decision: str,
    category: str | None = None,
    rule_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """
    Attach the guard outcome to the ``guardrail_input`` span before it is ended.

    ``decision`` is ``"allow"`` (classified SAFE) or ``"block"``; ``category`` /
    ``rule_id`` come from the block :class:`GuardrailDecision` (folded onto this
    one span — there is no longer a separate decision span). Token counts render
    the guard call's cost in Phoenix.
    """
    span.set_attribute("guardrail.decision", decision)
    if category is not None:
        span.set_attribute("guardrail.category", category)
    if rule_id is not None:
        span.set_attribute("guardrail.rule_id", rule_id)
    if input_tokens is not None:
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, input_tokens)
    if output_tokens is not None:
        span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, output_tokens)
