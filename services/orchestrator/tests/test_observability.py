"""Observability tests (Group 5): no-op degradation, timings, trace, spans.

Spans are captured with an in-memory exporter — no Phoenix, no network.
Focused checks only (per task 5.1) — exhaustive attribute-matrix tests are
intentionally skipped.
"""

import pytest
from app.clients import AppClients
from app.main import app as main_app
from app.schemas.contract_validation import validate_result
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_trace_id

from tests.conftest import FIXTURE_SETTINGS

VALID_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-obs-001",
    "pipeline_config": "legal-rag-default-1.1.0",
}

TIMING_KEYS = {
    "embedding",
    "retrieval",
    "context_assembly",
    "generation",
    "citation_build",
    "total",
}


@pytest.fixture
def traced_client(mock_bedrock, mock_search):
    """TestClient with mocked clients AND a real tracer over an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(bedrock=mock_bedrock, search=mock_search)
    main_app.state.tracer = provider.get_tracer("test-tracer")
    try:
        yield TestClient(main_app), exporter
    finally:
        del main_app.state.settings
        del main_app.state.clients
        del main_app.state.tracer


def test_unset_phoenix_endpoint_yields_a_fully_correct_response(
    monkeypatch, mock_bedrock, mock_search
):
    """REQUIRED: no PHOENIX_ENDPOINT → no-op tracer, service FULLY functional."""
    monkeypatch.delenv("PHOENIX_ENDPOINT", raising=False)
    # Lifespan runs for real (TestClient context manager): it builds the
    # no-op tracer from the fixture Settings (phoenix_endpoint=None); the
    # pre-installed mock clients are respected, never rebuilt.
    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(bedrock=mock_bedrock, search=mock_search)
    try:
        with TestClient(main_app) as client:
            response = client.post("/query", json=VALID_REQUEST)
    finally:
        del main_app.state.settings
        del main_app.state.clients
        del main_app.state.tracer
        del main_app.state.tracer_provider

    # Actual payload CORRECTNESS, not merely no-crash.
    assert response.status_code == 200
    body = response.json()
    validate_result(body["result"])
    assert body["generation_mode"] == "live"
    result = body["result"]
    assert result["answer"]["citations"], "citations must be built on the no-op path too"
    assert set(result["timings_ms"].keys()) == TIMING_KEYS
    # trace is OMITTED (not null) under the no-op tracer.
    assert "trace" not in result


def test_timings_ms_carries_exactly_the_stage_keys_with_nonnegative_numbers(client):
    """timings_ms: embedding/retrieval/context_assembly/generation/citation_build/total."""
    result = client.post("/query", json=VALID_REQUEST).json()["result"]

    timings = result["timings_ms"]
    assert set(timings.keys()) == TIMING_KEYS
    for key, value in timings.items():
        assert isinstance(value, int | float), key
        assert value >= 0, key


def test_trace_block_is_populated_when_the_tracer_is_real(traced_client):
    """result.trace carries trace_id/span_id/phoenix_project matching the export."""
    client, exporter = traced_client

    result = client.post("/query", json=VALID_REQUEST).json()["result"]

    # trace + timings_ms are DECLARED v1.1.0 slots: schema-valid when present.
    validate_result(result)
    trace_block = result["trace"]
    assert set(trace_block.keys()) == {"trace_id", "span_id", "phoenix_project"}
    assert trace_block["phoenix_project"] == "orchestrator"

    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "POST /query")
    assert trace_block["trace_id"] == format_trace_id(root.context.trace_id)
    # Every stage span belongs to the same trace as the echoed trace_id.
    assert {format_trace_id(span.context.trace_id) for span in spans} == {trace_block["trace_id"]}


def test_system_version_provenance_echo_is_schema_valid(client):
    """Index name + endpoint host ride in the OPEN system_version object."""
    result = client.post("/query", json=VALID_REQUEST).json()["result"]

    validate_result(result)  # the echo must never break schema conformance
    assert result["system_version"]["opensearch_index"] == "legal-rag-bench"
    assert result["system_version"]["opensearch_host"] == "search-legal.example.com"


def test_stage_spans_carry_the_specified_openinference_attributes(traced_client):
    """Retriever documents + index/host; LLM model/tokens; citation drop count."""
    client, exporter = traced_client
    client.post("/query", json=VALID_REQUEST)

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans.keys()) == {
        "POST /query",
        "embedding",
        "retrieval",
        "context_assembly",
        "generation",
        "citation_build",
    }

    embedding = spans["embedding"].attributes
    assert embedding["openinference.span.kind"] == "EMBEDDING"
    assert embedding["embedding.model_name"] == "amazon.titan-embed-text-v2:0"

    retrieval = spans["retrieval"].attributes
    assert retrieval["openinference.span.kind"] == "RETRIEVER"
    assert retrieval["retrieval.documents.0.document.id"] == "gst-act-1999:0"
    assert retrieval["retrieval.documents.1.document.id"] == "gst-act-1999:1"
    assert retrieval["retrieval.documents.0.document.score"] == 0.91
    # Q8: the retriever span alone must attribute the index + endpoint host.
    assert retrieval["opensearch.index"] == "legal-rag-bench"
    assert retrieval["opensearch.host"] == "search-legal.example.com"

    generation = spans["generation"].attributes
    assert generation["openinference.span.kind"] == "LLM"
    assert generation["llm.model_name"] == "au.anthropic.claude-sonnet-4-6"
    assert generation["llm.token_count.prompt"] == 321
    assert generation["llm.token_count.completion"] == 42

    citation = spans["citation_build"].attributes
    assert citation["citation.count"] == 2
    # The canned answer cites one hallucinated id ([made-up-doc:9]).
    assert citation["citation.dropped_unknown_marker_count"] == 1


# ---------------------------------------------------------------------------
# W3C traceparent extraction (chainlit-chat-ui Task Group 4). Focused checks
# only (per task 4.1) — sampling and tracestate-content tests are skipped
# (the propagator default passes tracestate through).
# ---------------------------------------------------------------------------

CALLER_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
CALLER_SPAN_ID = "b7ad6b7169203331"
CALLER_TRACEPARENT = {"traceparent": f"00-{CALLER_TRACE_ID}-{CALLER_SPAN_ID}-01"}

STREAM_REQUEST = {**VALID_REQUEST, "query_id": "q-obs-stream-001"}


def _root_span(exporter, name):
    return next(span for span in exporter.get_finished_spans() if span.name == name)


def test_traceparent_makes_the_pipeline_root_a_child_of_the_caller_on_both_routes(
    traced_client,
):
    """Supplied traceparent → same trace id, parent span id matches the caller."""
    client, exporter = traced_client

    blocking = client.post("/query", json=VALID_REQUEST, headers=CALLER_TRACEPARENT)
    assert blocking.status_code == 200
    root = _root_span(exporter, "POST /query")
    assert format_trace_id(root.context.trace_id) == CALLER_TRACE_ID
    assert f"{root.parent.span_id:016x}" == CALLER_SPAN_ID
    # result.trace still echoes the PIPELINE root (now a child of the caller).
    trace_block = blocking.json()["result"]["trace"]
    assert trace_block["trace_id"] == CALLER_TRACE_ID
    assert trace_block["span_id"] == f"{root.context.span_id:016x}"

    exporter.clear()
    streamed = client.post("/query/stream", json=STREAM_REQUEST, headers=CALLER_TRACEPARENT)
    assert streamed.status_code == 200
    stream_root = _root_span(exporter, "POST /query/stream")
    assert format_trace_id(stream_root.context.trace_id) == CALLER_TRACE_ID
    assert f"{stream_root.parent.span_id:016x}" == CALLER_SPAN_ID
    # The final envelope's trace echo carries the joined trace id too.
    final_data = streamed.text.split("event: final\ndata: ", 1)[1].split("\n\n", 1)[0]
    import json as _json

    assert _json.loads(final_data)["result"]["trace"]["trace_id"] == CALLER_TRACE_ID


def test_absent_or_garbage_traceparent_yields_a_fresh_valid_root(traced_client):
    """No header, or an invalid one → exactly today's behavior: a fresh root."""
    client, exporter = traced_client

    client.post("/query", json=VALID_REQUEST)
    absent_root = _root_span(exporter, "POST /query")
    assert absent_root.parent is None
    assert absent_root.context.trace_id != 0

    exporter.clear()
    client.post("/query", json=VALID_REQUEST, headers={"traceparent": "not-a-valid-traceparent"})
    garbage_root = _root_span(exporter, "POST /query")
    assert garbage_root.parent is None
    assert garbage_root.context.trace_id != 0
    assert format_trace_id(garbage_root.context.trace_id) != CALLER_TRACE_ID

    exporter.clear()
    client.post("/query/stream", json=STREAM_REQUEST, headers={"traceparent": "00-garbage"})
    stream_root = _root_span(exporter, "POST /query/stream")
    assert stream_root.parent is None
    assert stream_root.context.trace_id != 0


def test_result_trace_stays_omitted_under_the_noop_tracer_even_with_traceparent(client):
    """The no-op degradation contract survives extraction on both routes."""
    blocking = client.post("/query", json=VALID_REQUEST, headers=CALLER_TRACEPARENT)
    assert blocking.status_code == 200
    assert "trace" not in blocking.json()["result"]

    streamed = client.post("/query/stream", json=STREAM_REQUEST, headers=CALLER_TRACEPARENT)
    assert streamed.status_code == 200
    assert "event: final" in streamed.text
    final_data = streamed.text.split("event: final\ndata: ", 1)[1].split("\n\n", 1)[0]
    import json as _json

    assert "trace" not in _json.loads(final_data)["result"]
