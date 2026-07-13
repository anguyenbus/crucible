"""Wired-pipeline tests (Group 4): mocked clients, real stage chain, no AWS.

Focused checks only (per task 4.1) — exhaustive failure-permutation tests are
intentionally skipped.
"""

from pathlib import Path

from app.clients import AppClients
from app.clients.errors import BedrockThrottleExhaustedError
from app.main import app as main_app
from app.schemas.contract_validation import validate_result
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

from tests.conftest import CANNED_ANSWER_TEXT, CANNED_CHUNKS, FIXTURE_SETTINGS

VALID_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-live-001",
    "pipeline_config": "legal-rag-default-1.1.0",
}


def test_query_runs_the_live_chain_and_returns_schema_valid_result(client, mock_bedrock):
    """retrieve → assemble → prompt → generate → cite, generation_mode='live'."""
    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200

    body = response.json()
    assert body["generation_mode"] == "live"
    assert body["guardrail_decisions"] == []
    validate_result(body["result"])

    result = body["result"]
    # Chunks come from the (mocked) retriever, not any canned router path.
    assert result["retrieved_chunks"] == CANNED_CHUNKS
    # answer.text is the generated text with markers KEPT in it.
    assert result["answer"]["text"] == CANNED_ANSWER_TEXT
    # Citations reference actually retrieved chunk ids; the hallucinated
    # marker ([made-up-doc:9]) was dropped, never an error.
    cited = [cid for c in result["answer"]["citations"] for cid in c["chunk_ids"]]
    assert cited == ["gst-act-1999:0", "gst-act-1999:1"]
    # The generation prompt was built from the assembled context blocks and
    # the (identity-routed/rewritten) question — the chain is truly wired.
    prompt = mock_bedrock.generate_calls[0]["prompt"]
    assert "[gst-act-1999:0]: Section 38-190" in prompt
    assert VALID_REQUEST["question"] in prompt


def test_system_version_carries_pins_and_the_q8_provenance_echo(client):
    """pipeline_version/config_sha256 + model pins + resolved index and host."""
    from app.config import resolve_pipeline_config

    system_version = client.post("/query", json=VALID_REQUEST).json()["result"]["system_version"]
    resolved = resolve_pipeline_config("legal-rag-default-1.1.0")
    assert system_version["pipeline_version"] == "1.1.0"
    assert system_version["config_sha256"] == resolved.config_sha256
    assert system_version["generator_model"] == "au.anthropic.claude-sonnet-4-6"
    assert system_version["embedder_model"] == "amazon.titan-embed-text-v2:0"
    # Q8 binding addition: identical config_sha256 against a different index
    # must stay attributable — echo the resolved index name + endpoint host.
    assert system_version["opensearch_index"] == "legal-rag-bench"
    assert system_version["opensearch_host"] == "search-legal.example.com"


def test_bedrock_throttle_exhausted_maps_to_503_with_retry_after(client, mock_bedrock):
    """Throttle exhaustion → 503, Retry-After header, dependency='bedrock'."""
    mock_bedrock.generate_error = BedrockThrottleExhaustedError(
        "Bedrock throttling persisted after 5 retries."
    )

    response = client.post("/query", json=VALID_REQUEST)

    assert response.status_code == 503
    assert response.headers["Retry-After"].isdigit()
    assert response.json()["dependency"] == "bedrock"


def test_dependency_failures_map_to_502_and_never_leak_raw_exceptions(
    client, mock_bedrock, mock_search
):
    """OpenSearch failure and non-throttle Bedrock ClientError → clean 502s."""
    mock_search.search_error = OpenSearchConnectionError(
        "N/A", "RAW-OPENSEARCH-INTERNALS", Exception("socket gore")
    )
    opensearch_response = client.post("/query", json=VALID_REQUEST)
    assert opensearch_response.status_code == 502
    assert opensearch_response.json()["dependency"] == "opensearch"
    assert "RAW-OPENSEARCH-INTERNALS" not in opensearch_response.text

    mock_search.search_error = None
    mock_bedrock.generate_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "RAW-BOTO-INTERNALS"}},
        "InvokeModel",
    )
    bedrock_response = client.post("/query", json=VALID_REQUEST)
    assert bedrock_response.status_code == 502
    assert bedrock_response.json()["dependency"] == "bedrock"
    assert "RAW-BOTO-INTERNALS" not in bedrock_response.text


def test_unconstructed_search_client_maps_to_502_naming_opensearch(mock_bedrock):
    """A not-ready OpenSearch client (e.g. _meta mismatch at init) → 502."""
    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(
        bedrock=mock_bedrock,
        search=None,
        opensearch_unavailable_reason="Embedder mismatch for index 'legal-rag-bench'.",
    )
    try:
        response = TestClient(main_app).post("/query", json=VALID_REQUEST)
    finally:
        del main_app.state.settings
        del main_app.state.clients

    assert response.status_code == 502
    assert response.json()["dependency"] == "opensearch"


def test_unknown_config_404_and_malformed_ref_422_carry_forward(client):
    """Config-resolution error mapping is unchanged from Phase 1."""
    unknown = client.post(
        "/query", json={**VALID_REQUEST, "pipeline_config": "no-such-config-9.9.9"}
    )
    assert unknown.status_code == 404
    assert "no-such-config-9.9.9" in unknown.json()["detail"]

    malformed = client.post("/query", json={**VALID_REQUEST, "pipeline_config": "not_a_ref"})
    assert malformed.status_code == 422


def test_no_reachable_stub_mode_remains_in_the_app(client):
    """'stub' survives ONLY as the envelope enum value — no reachable stub path."""
    # Every response is live; no env flag or config ref flips it.
    assert client.post("/query", json=VALID_REQUEST).json()["generation_mode"] == "live"

    # Source-level tripwire: the only place the string "stub" appears as a
    # generation_mode VALUE in app/ is the envelope's Literal enum.
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if '"stub"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == ["schemas/envelope.py"]
