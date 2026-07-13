"""Cross-cutting end-to-end gap tests for the chainlit-chat-ui feature (Task Group 11).

These fill the highest-value WORKFLOW gaps left by the per-group suites — the
combinations no single group owned:
  (a) a full mocked MULTI-TURN flow over ``/query/stream`` — ``history`` in →
      the history-prefixed rewrite is what embedding/retrieval run on → the
      prompt carries the history block → the streamed ``final`` envelope's
      ``result`` passes ``validate_result`` and echoes the CURRENT question;
  (b) an SSE stream whose FIRST Bedrock event fails after the 200 is committed
      (initial-call retry exhausted) → exactly ONE ``error`` event with
      ``http_equivalent`` 503 (NOT an HTTP 503), never a ``token`` or ``final``;
  (c) ``history`` + W3C ``traceparent`` together on ``/query/stream`` — the exact
      shape the Chainlit UI sends — producing a child-of-caller root span AND a
      valid ``final`` envelope whose trace echo carries the joined trace id.

Mocked clients only — no AWS. Distinct from ``tests/test_gap_review.py`` (that
file belongs to the prior walking-skeleton spec's non-streaming path).
"""

from __future__ import annotations

import pytest
from app.clients import AppClients
from app.clients.errors import BedrockThrottleExhaustedError
from app.main import app as main_app
from app.schemas.contract_validation import validate_result
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_trace_id

from tests.conftest import CANNED_ANSWER_TEXT, FIXTURE_SETTINGS
from tests.test_query_stream import parse_sse

# The exact multi-turn request shape the Chainlit UI sends: the 1.2.0
# (history-aware) config, a prior user+assistant turn, and a follow-up whose
# meaning depends on the history ("it" = the earlier supply).
MULTI_TURN_REQUEST = {
    "question": "Does it also apply to digital services?",
    "query_id": "q-gap-multi-001",
    "pipeline_config": "legal-rag-default-1.2.0",
    "history": [
        {"role": "user", "text": "Is a supply to a non-resident GST-free?"},
        {"role": "assistant", "text": "Yes, under section 38-190 [gst-act-1999:0]."},
    ],
}

# The deterministic rewrite prefixes the recent USER turn(s) to the current
# question (oldest-first); the assistant turn is never selected for retrieval.
REWRITTEN_QUERY = "Is a supply to a non-resident GST-free?\nDoes it also apply to digital services?"

CALLER_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
CALLER_SPAN_ID = "b7ad6b7169203331"
CALLER_TRACEPARENT = {"traceparent": f"00-{CALLER_TRACE_ID}-{CALLER_SPAN_ID}-01"}


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


def test_streamed_multi_turn_flow_rewrites_retrieval_prompts_history_and_validates(
    client, mock_bedrock
):
    """history → rewritten retrieval query + prompt history block → valid final envelope."""
    events = parse_sse(client.post("/query/stream", json=MULTI_TURN_REQUEST).text)

    names = [name for name, _ in events]
    assert names.count("final") == 1 and "error" not in names
    assert names[-1] == "final", "token* then exactly one terminal (final)"

    # The history-prefixed rewrite is what was embedded AND retrieved against —
    # the honest provenance channel for what the pipeline actually searched.
    assert mock_bedrock.embed_calls[-1]["text"] == REWRITTEN_QUERY

    # The prompt carries the history BLOCK plus the CURRENT question — never the
    # history-prefixed retrieval query.
    prompt = mock_bedrock.generate_stream_calls[-1]["prompt"]
    assert "Conversation so far:\nUser: Is a supply to a non-resident GST-free?" in prompt
    assert "Question: Does it also apply to digital services?" in prompt

    # The streamed final envelope is a valid v1.1.0 result; history is NEVER
    # echoed — query.text is the CURRENT question verbatim.
    final = events[-1][1]
    validate_result(final["result"])
    assert final["generation_mode"] == "live"
    assert final["result"]["query"]["text"] == MULTI_TURN_REQUEST["question"]
    assert final["result"]["answer"]["text"] == CANNED_ANSWER_TEXT


def test_initial_stream_call_throttle_exhaustion_is_an_error_event_not_an_http_503(
    client, mock_bedrock
):
    """First Bedrock event fails after the 200 → ONE error event (503-equiv), no token/final."""
    # generate_error fires on the INITIAL invoke_model_with_response_stream call
    # (retry exhausted before any delta) — but the 200 is already committed, so
    # this becomes a typed SSE error event, never a plain HTTP 503.
    mock_bedrock.generate_error = BedrockThrottleExhaustedError(
        "Bedrock throttling persisted after 5 retries."
    )
    response = client.post("/query/stream", json=MULTI_TURN_REQUEST)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == ["error"], "no token and no final ever follow the initial-call failure"

    error = events[0][1]
    assert error["http_equivalent"] == 503
    assert error["dependency"] == "bedrock"
    assert isinstance(error["retry_after_seconds"], int)
    assert "throttling" in error["detail"].lower()
    # The stream was genuinely attempted (initial call), not short-circuited pre-commit.
    assert len(mock_bedrock.generate_stream_calls) == 1


def test_streamed_history_plus_traceparent_joins_the_caller_and_yields_valid_final(
    traced_client,
):
    """The exact UI call (history + traceparent) → child-of-caller root + valid final."""
    tclient, exporter = traced_client

    response = tclient.post("/query/stream", json=MULTI_TURN_REQUEST, headers=CALLER_TRACEPARENT)
    assert response.status_code == 200

    # The pipeline root span joined the caller's trace as a child.
    root = next(span for span in exporter.get_finished_spans() if span.name == "POST /query/stream")
    assert format_trace_id(root.context.trace_id) == CALLER_TRACE_ID
    assert f"{root.parent.span_id:016x}" == CALLER_SPAN_ID

    # Retrieval still ran on the history-prefixed rewrite (memory survives the join).
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["retrieval"].attributes["input.value"] == REWRITTEN_QUERY

    # The final envelope validates and its trace echo carries the JOINED trace id.
    events = parse_sse(response.text)
    assert events[-1][0] == "final"
    final = events[-1][1]
    validate_result(final["result"])
    assert final["result"]["trace"]["trace_id"] == CALLER_TRACE_ID
    assert final["result"]["query"]["text"] == MULTI_TURN_REQUEST["question"]
