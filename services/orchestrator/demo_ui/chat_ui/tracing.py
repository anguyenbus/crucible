"""
The UI's OWN OTel tracer + the per-message CHAIN span + traceparent injection.

This copies the PATTERN of the orchestrator's
``app/observability.py::configure_tracer`` — it never imports it (dependency
firewall). Decisions it encodes:

- SINGLE SHARED Phoenix project: resource ``PROJECT_NAME = "orchestrator"``
  (same as the service) with a distinct ``service.name = "chainlit-ui"``.
  Phoenix groups traces by the project resource attribute — splitting
  projects would render each joined trace partially in each view.
- OTLP-HTTP to ``{PHOENIX_ENDPOINT}/v1/traces`` (Phoenix accepts OTLP over
  HTTP on its UI port; no gRPC stack).
- ``PHOENIX_ENDPOINT`` unset → a genuine ``NoOpTracer`` and the UI is FULLY
  functional; ``inject_trace_headers`` then injects NOTHING (a non-recording
  span has an INVALID context — there is no traceparent worth sending), so
  the orchestrator starts a fresh root exactly as today.
- One OpenInference CHAIN span per chat message (``INPUT_VALUE`` = user
  message, ``OUTPUT_VALUE`` = final answer text); ``traceparent`` injected on
  the ``/query/stream`` call makes the pipeline root its child — ONE joined
  Phoenix trace per turn.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from openinference.semconv.resource import ResourceAttributes
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.propagate import inject
from opentelemetry.trace import NoOpTracer, Span, Tracer

# The Phoenix project SHARED with the orchestrator service (Q7 decision).
PHOENIX_PROJECT = "orchestrator"

# Distinct service.name so the UI's spans are attributable within the trace.
SERVICE_NAME = "chainlit-ui"

_TRACER_NAME = "demo-ui.chat"


def configure_tracer(phoenix_endpoint: str | None) -> tuple[Tracer, Any | None]:
    """
    Build the UI tracer (module-load time in ``app.py``; pattern-copied).

    Args:
        phoenix_endpoint: Phoenix UI endpoint (e.g. ``http://localhost:6006``)
            or ``None`` when ``PHOENIX_ENDPOINT`` is unset.

    Returns:
        ``(tracer, provider)`` — a real OTLP-exporting tracer plus its
        provider (so shutdown can flush), or ``(NoOpTracer(), None)`` when
        Phoenix is not configured.

    """
    if not phoenix_endpoint:
        return NoOpTracer(), None

    # SDK imports stay inside the configured branch so the no-op path never
    # touches exporter machinery (same discipline as the service).
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            ResourceAttributes.PROJECT_NAME: PHOENIX_PROJECT,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{phoenix_endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(_TRACER_NAME), provider


@contextmanager
def chat_turn_span(tracer: Tracer, *, question: str) -> Iterator[Span]:
    """One OpenInference CHAIN span per chat message (INPUT_VALUE = message)."""
    with tracer.start_as_current_span(
        "chat_turn",
        attributes={
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
            SpanAttributes.INPUT_VALUE: question,
        },
    ) as span:
        yield span


def set_turn_output(span: Span, answer_text: str) -> None:
    """Attach the final answer text as the CHAIN span's OUTPUT_VALUE."""
    span.set_attribute(SpanAttributes.OUTPUT_VALUE, answer_text)


def inject_trace_headers() -> dict[str, str]:
    """
    Headers carrying the current W3C trace context for the streaming call.

    Uses ``opentelemetry.propagate.inject`` over the AMBIENT context: inside
    a real ``chat_turn_span`` this adds ``traceparent`` (the pipeline root
    joins the UI trace); under the no-op tracer the current span context is
    invalid and the propagator injects nothing — the dict stays empty and the
    UI remains fully functional.
    """
    headers: dict[str, str] = {}
    inject(headers)
    return headers
