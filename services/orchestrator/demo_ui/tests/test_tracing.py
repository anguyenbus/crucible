"""Tracing bootstrap tests: no-op degradation, resource, traceparent inject."""

import re

from chat_ui.tracing import chat_turn_span, configure_tracer, inject_trace_headers
from openinference.semconv.resource import ResourceAttributes
from opentelemetry import trace as trace_api
from opentelemetry.trace import NoOpTracer

_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def test_unset_phoenix_endpoint_is_a_genuine_noop_and_injects_no_traceparent():
    tracer, provider = configure_tracer(None)

    assert isinstance(tracer, NoOpTracer)
    assert provider is None

    # The UI logic stays fully functional: the CHAIN span context manager
    # works, and the request path injects NO traceparent (a non-recording
    # span has an invalid context — nothing worth injecting).
    with chat_turn_span(tracer, question="q") as span:
        headers = inject_trace_headers()
        assert not span.is_recording()
    assert "traceparent" not in headers
    assert headers == {}


def test_configured_provider_carries_shared_project_and_distinct_service_name():
    tracer, provider = configure_tracer("http://localhost:6006")
    try:
        attributes = provider.resource.attributes
        # SINGLE SHARED Phoenix project with the orchestrator service...
        assert attributes[ResourceAttributes.PROJECT_NAME] == "orchestrator"
        # ...but a distinct service.name so UI spans stay attributable.
        assert attributes["service.name"] == "chainlit-ui"
        assert not isinstance(tracer, NoOpTracer)
    finally:
        provider.shutdown()


def test_active_span_injects_valid_traceparent_on_outgoing_headers():
    tracer, provider = configure_tracer("http://127.0.0.1:9")  # never exported
    try:
        span = tracer.start_span("chat_turn")
        # end_on_exit=False keeps the span out of the export queue, so the
        # provider shuts down without flushing to the dead endpoint.
        with trace_api.use_span(span, end_on_exit=False):
            headers = inject_trace_headers()
        assert _TRACEPARENT_RE.fullmatch(headers["traceparent"])
        context = span.get_span_context()
        assert format(context.trace_id, "032x") in headers["traceparent"]
    finally:
        provider.shutdown()
