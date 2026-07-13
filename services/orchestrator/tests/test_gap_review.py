"""Test-review gap fills (Group 9): strategic additions only, no permutations.

Coverage review of Groups 1-8 against the spec's 10 requirement blocks found
the suite strong on the happy path, span attributes, provenance echo, timing
keys and the 404/422/500/502/503(+Retry-After) taxonomy. The REAL gaps filled
here (mocked clients, zero network):

1. Zero-marker answer  → empty ``citations`` yet schema-valid (declared eval
   behavior for unsupported answers).
2. Unknown (dropped) markers still ADVANCE the ``claim_span`` boundary — the
   citation builder's documented invariant, previously untested.
3. ``timings_ms.total`` >= sum of the stage timings (the stage intervals are
   disjoint sub-intervals of the total).
4. Throttle exhaustion in the EMBEDDING call maps to 503 too (only the
   generation call's throttle path was covered).
5. An ABSENT ``query_id`` is generated server-side (only verbatim echo was
   covered).
6. ZERO retrieval hits still yield a schema-valid live response with empty
   ``retrieved_chunks``/``citations`` (all markers drop against an empty set).
"""

import uuid

from app.clients.errors import BedrockThrottleExhaustedError
from app.orchestrator.citation_builder import build_citations
from app.schemas.contract_validation import validate_result

from tests.conftest import CANNED_GENERATION

VALID_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-gap-001",
    "pipeline_config": "legal-rag-default-1.1.0",
}


def test_zero_marker_answer_yields_empty_citations_and_schema_valid_result(client, mock_bedrock):
    """An answer citing nothing produces citations == [] — valid, never an error."""
    mock_bedrock.generation_result = CANNED_GENERATION.__class__(
        text="The corpus does not support an answer to this question.",
        model_id=CANNED_GENERATION.model_id,
        input_tokens=100,
        output_tokens=12,
        stop_reason="end_turn",
    )

    response = client.post("/query", json=VALID_REQUEST)

    assert response.status_code == 200
    result = response.json()["result"]
    validate_result(result)
    assert result["answer"]["citations"] == []
    # The retrieved set is still fully reported (eval needs it for recall).
    assert len(result["retrieved_chunks"]) == 2


def test_dropped_unknown_marker_still_advances_the_claim_span_boundary():
    """Text attributed to a hallucinated marker is never re-attributed onward."""
    first, unknown, second = "[actA:0]", "[made-up:9]", "[actB:1]"
    answer = f"Claim one {first}. Hallucinated {unknown}. Claim two {second}."
    first_end = answer.index(first) + len(first)
    unknown_end = answer.index(unknown) + len(unknown)
    second_end = answer.index(second) + len(second)

    result = build_citations(answer, {"actA:0", "actB:1"})

    assert result.dropped_unknown_marker_count == 1
    # The second citation starts at the end of the DROPPED marker, not the end
    # of the first kept one — the boundary advanced on the unknown marker.
    assert result.citations == [
        {"claim_span": [0, first_end], "chunk_ids": ["actA:0"]},
        {"claim_span": [unknown_end, second_end], "chunk_ids": ["actB:1"]},
    ]


def test_timings_total_is_at_least_the_sum_of_the_stage_timings(client):
    """total spans the whole request; stage intervals are disjoint slices of it."""
    timings = client.post("/query", json=VALID_REQUEST).json()["result"]["timings_ms"]

    stage_sum = sum(value for key, value in timings.items() if key != "total")
    assert timings["total"] >= stage_sum


def test_embedding_throttle_exhausted_also_maps_to_503_with_retry_after(client, mock_bedrock):
    """The 503 taxonomy covers the EMBED call, not just the generate call."""
    mock_bedrock.embed_error = BedrockThrottleExhaustedError(
        "Bedrock throttling persisted after 5 retries."
    )

    response = client.post("/query", json=VALID_REQUEST)

    assert response.status_code == 503
    assert response.headers["Retry-After"].isdigit()
    assert response.json()["dependency"] == "bedrock"


def test_absent_query_id_is_generated_server_side(client):
    """No query_id in the request → the required field is filled with a UUID."""
    request = {key: value for key, value in VALID_REQUEST.items() if key != "query_id"}

    result = client.post("/query", json=request).json()["result"]

    validate_result(result)  # query.query_id is REQUIRED by the schema
    generated = result["query"]["query_id"]
    assert uuid.UUID(generated)  # non-empty, well-formed, never echo of None


def test_zero_retrieval_hits_still_yield_a_schema_valid_live_response(client, mock_search):
    """An empty hit list runs the whole chain: no chunks, no citations, still 200."""
    mock_search.search_hits = []

    response = client.post("/query", json=VALID_REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert body["generation_mode"] == "live"
    result = body["result"]
    validate_result(result)
    assert result["retrieved_chunks"] == []
    # Every marker in the canned answer is unknown against an empty retrieved
    # set: all are dropped (never an error) and citations stay empty.
    assert result["answer"]["citations"] == []
