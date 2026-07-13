"""``POST /query/stream`` SSE tests (chainlit-chat-ui Task Group 3).

Mocked clients; the ``text/event-stream`` body is parsed directly. Binding
contract under test: ``token``* then EXACTLY ONE terminal event (``final`` or
``error``) — silent truncation is forbidden. Focused checks only (per task
3.1) — exhaustive delta-content permutations are intentionally skipped.
"""

from __future__ import annotations

import json

import pytest
from app.clients import AppClients
from app.clients.errors import BedrockThrottleExhaustedError
from app.main import app as main_app
from app.schemas.contract_validation import validate_result
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode, format_trace_id

from tests.conftest import CANNED_ANSWER_TEXT, FIXTURE_SETTINGS, canned_stream_deltas

VALID_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-stream-001",
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


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse a text/event-stream body into (event, data) pairs, in order."""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        assert event_name is not None, f"SSE block without an event name: {block!r}"
        events.append((event_name, json.loads("\n".join(data_lines))))
    return events


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


def test_stream_emits_raw_token_events_then_exactly_one_final(client):
    """token* → exactly ONE final; deltas forwarded raw, markers unresolved."""
    response = client.post("/query/stream", json=VALID_REQUEST)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == ["token", "token", "token", "final"], "token* then exactly one terminal"
    assert names.count("final") + names.count("error") == 1

    # Each token carries {"text": <delta>}, forwarded RAW as received —
    # citation markers included, unresolved.
    deltas = [data["text"] for name, data in events if name == "token"]
    assert deltas == canned_stream_deltas()
    assert "".join(deltas) == CANNED_ANSWER_TEXT


def test_final_envelope_is_identical_in_shape_to_the_blocking_response(client):
    """final carries the full QueryResponse envelope; result passes v1.1.0."""
    blocking = client.post("/query", json=VALID_REQUEST).json()
    events = parse_sse(client.post("/query/stream", json=VALID_REQUEST).text)
    final = events[-1][1]

    assert set(final.keys()) == {"result", "guardrail_decisions", "generation_mode"}
    assert final["guardrail_decisions"] == []
    assert final["generation_mode"] == "live"
    validate_result(final["result"])

    # Citations are built over the FULL accumulated text — identical to the
    # blocking path's citations for the same mocked generation.
    result = final["result"]
    assert result["answer"]["text"] == CANNED_ANSWER_TEXT
    assert result["answer"]["citations"] == blocking["result"]["answer"]["citations"]
    assert result["retrieved_chunks"] == blocking["result"]["retrieved_chunks"]
    assert result["query"]["query_id"] == "q-stream-001"
    assert set(result["timings_ms"].keys()) == TIMING_KEYS


def test_mid_stream_failure_yields_exactly_one_typed_error_event(client, mock_bedrock):
    """Mid-stream throttle/ClientError → ONE error event, NO final, stream closes."""
    # Throttle exhaustion after one delta → 503-equivalent with retry guidance.
    mock_bedrock.stream_error = BedrockThrottleExhaustedError(
        "Bedrock throttling persisted after 5 retries."
    )
    mock_bedrock.stream_error_after_deltas = 1
    events = parse_sse(client.post("/query/stream", json=VALID_REQUEST).text)

    names = [name for name, _ in events]
    assert names == ["token", "error"], "exactly one terminal event; never a final after error"
    error = events[-1][1]
    assert error["dependency"] == "bedrock"
    assert error["http_equivalent"] == 503
    assert isinstance(error["retry_after_seconds"], int)
    assert "throttling" in error["detail"].lower()

    # Non-throttle ClientError mid-stream → 502-equivalent; raw text never leaks.
    mock_bedrock.stream_error = ClientError(
        {"Error": {"Code": "ModelStreamErrorException", "Message": "RAW-BOTO-INTERNALS"}},
        "InvokeModelWithResponseStream",
    )
    events = parse_sse(client.post("/query/stream", json=VALID_REQUEST).text)
    names = [name for name, _ in events]
    assert names == ["token", "error"]
    error = events[-1][1]
    assert error["dependency"] == "bedrock"
    assert error["http_equivalent"] == 502
    assert "retry_after_seconds" not in error
    assert "ModelStreamErrorException" in error["detail"]
    assert "RAW-BOTO-INTERNALS" not in json.dumps(error)


def test_pre_stream_failures_return_plain_http_errors_never_an_sse_body(
    client, mock_bedrock, mock_search
):
    """404/422/502/503 raised BEFORE streaming reuse the app-level handlers."""
    unknown = client.post(
        "/query/stream", json={**VALID_REQUEST, "pipeline_config": "no-such-config-9.9.9"}
    )
    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/json")
    assert "no-such-config-9.9.9" in unknown.json()["detail"]

    malformed = client.post("/query/stream", json={**VALID_REQUEST, "pipeline_config": "not_a_ref"})
    assert malformed.status_code == 422
    assert malformed.headers["content-type"].startswith("application/json")

    from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

    mock_search.search_error = OpenSearchConnectionError("N/A", "boom", Exception("boom"))
    search_down = client.post("/query/stream", json=VALID_REQUEST)
    assert search_down.status_code == 502
    assert search_down.json()["dependency"] == "opensearch"
    mock_search.search_error = None

    mock_bedrock.embed_error = BedrockThrottleExhaustedError("Embedding throttled out.")
    embed_throttled = client.post("/query/stream", json=VALID_REQUEST)
    assert embed_throttled.status_code == 503
    assert embed_throttled.headers["Retry-After"].isdigit()
    assert embed_throttled.json()["dependency"] == "bedrock"


def test_stream_spans_and_timings_are_recorded_as_on_the_blocking_path(traced_client):
    """generation span wraps the WHOLE stream; token counts attached; root named."""
    client, exporter = traced_client
    events = parse_sse(client.post("/query/stream", json=VALID_REQUEST).text)

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans.keys()) == {
        "POST /query/stream",
        "embedding",
        "retrieval",
        "context_assembly",
        "generation",
        "citation_build",
    }

    root = spans["POST /query/stream"]
    for name in ("embedding", "retrieval", "context_assembly", "generation", "citation_build"):
        assert spans[name].parent.span_id == root.context.span_id, name
        assert spans[name].context.trace_id == root.context.trace_id, name

    # Token counts come from the client's post-stream plain data; the span
    # wraps the ENTIRE stream consumption (it ends after the last delta).
    generation = spans["generation"]
    assert generation.attributes["llm.token_count.prompt"] == 321
    assert generation.attributes["llm.token_count.completion"] == 42
    assert generation.attributes["output.value"] == CANNED_ANSWER_TEXT
    assert generation.end_time <= spans["citation_build"].start_time

    # timings_ms + trace echo land in the final envelope from real spans.
    final = events[-1][1]
    result = final["result"]
    assert set(result["timings_ms"].keys()) == TIMING_KEYS
    assert result["trace"]["trace_id"] == format_trace_id(root.context.trace_id)
    assert result["trace"]["phoenix_project"] == "orchestrator"


def test_mid_stream_error_marks_the_generation_span_and_ends_the_root_cleanly(
    traced_client, mock_bedrock
):
    """On error: generation span records the failure; root span still ends."""
    client, exporter = traced_client
    mock_bedrock.stream_error = BedrockThrottleExhaustedError("throttled mid-stream")
    events = parse_sse(client.post("/query/stream", json=VALID_REQUEST).text)
    assert [name for name, _ in events][-1] == "error"

    spans = {span.name: span for span in exporter.get_finished_spans()}
    generation = spans["generation"]
    assert generation.status.status_code is StatusCode.ERROR
    assert any(event.name == "exception" for event in generation.events)

    # The root span ended cleanly (it is in the FINISHED spans) — no leak.
    root = spans["POST /query/stream"]
    assert root.end_time is not None
    assert generation.parent.span_id == root.context.span_id
