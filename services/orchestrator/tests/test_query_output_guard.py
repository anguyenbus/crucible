"""Route-level output-guard tests (output+input-hardening Task Group 7).

Mocked clients; the output-guard-enabling config ``legal-rag-default-1.4.0``
(``output_categories=[pii, secrets]``). Binding contract on BOTH routes:

- a SECRETS answer ⇒ a 200 canned refusal (one output-stage ``block`` decision,
  empty citations), NEVER a 5xx — reusing the input block's refusal path;
- a redactable PII answer ⇒ 200 with the MASKED text, citations built over the
  redacted answer, and one ``transform`` decision (was an empty list);
- an advisory email/phone answer ⇒ 200, text UNCHANGED, one ``flag`` decision;
- a clean answer ⇒ 200, empty ``guardrail_decisions``, citations intact;
- on ``/query/stream`` (buffered because ``output_categories`` is non-empty) a
  secrets/PII answer emits ZERO ``token`` events and exactly ONE ``final``;
- ``1.1.0`` ``/query/stream`` still LIVE-streams token deltas byte-for-byte
  (empty ``output_categories`` ⇒ the live path is untouched).

Focused checks only (per task 7.1). No network access — the output guard is
regex-only and the generation is injected via the mock Bedrock client.
"""

from __future__ import annotations

import json

import pytest
from app.clients import AppClients
from app.clients.bedrock import GenerationResult
from app.main import app as main_app
from app.orchestrator.guardrails import REFUSAL_TEXT
from app.schemas.contract_validation import validate_result
from fastapi.testclient import TestClient

from tests.conftest import (
    CANNED_ANSWER_TEXT,
    CANNED_DOC_ID,
    FIXTURE_SETTINGS,
    canned_stream_deltas,
)

# The output-guard-enabling config: output_categories=[pii, secrets].
GUARD_CONFIG = "legal-rag-default-1.4.0"
# A benign legal question that misses the input pre-filter (no classifier call).
BENIGN_QUESTION = "Is a supply of legal services to a non-resident GST-free?"

# Injected answers (the mock returns these verbatim from generate/stream).
SECRETS_ANSWER = f"Per the retrieved config the API key is sk-ant-{'a' * 48} [{CANNED_DOC_ID}:0]."
PII_ANSWER = (
    f"The taxpayer's SSN 123-45-6789 appears in section 38-190 [{CANNED_DOC_ID}:0]. "
    f"Division 38 confirms the treatment [{CANNED_DOC_ID}:1]."
)
FLAG_ANSWER = (
    f"Contact the firm at legal@example.com about the GST-free supply [{CANNED_DOC_ID}:0]."
)


def _gen(text: str) -> GenerationResult:
    return GenerationResult(
        text=text,
        model_id="au.anthropic.claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=20,
        stop_reason="end_turn",
    )


def _client(mock_bedrock, mock_search):
    """Install mock clients (with a benign classifier) via the app.state seam."""

    class _SafeClassifier:
        def classify(self, question, *, model_id):  # pragma: no cover - never hit
            raise AssertionError("benign question must miss the input pre-filter")

    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(
        bedrock=mock_bedrock, search=mock_search, classifier=_SafeClassifier()
    )
    return TestClient(main_app)


@pytest.fixture
def _cleanup_state():
    yield
    for attr in ("settings", "clients", "tracer"):
        if hasattr(main_app.state, attr):
            delattr(main_app.state, attr)


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name, data_lines = None, []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        events.append((name, json.loads("\n".join(data_lines))))
    return events


# --- Non-streaming POST /query --------------------------------------------


def test_secrets_answer_on_query_returns_200_canned_refusal_with_one_block(
    mock_bedrock, mock_search, _cleanup_state
):
    mock_bedrock.generation_result = _gen(SECRETS_ANSWER)
    client = _client(mock_bedrock, mock_search)

    response = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["answer"]["text"] == REFUSAL_TEXT
    assert body["result"]["answer"]["citations"] == []
    assert body["result"]["retrieved_chunks"] == []
    assert len(body["guardrail_decisions"]) == 1
    decision = body["guardrail_decisions"][0]
    assert decision["stage"] == "output"
    assert decision["decision"] == "block"
    assert decision["category"] == "secrets"
    assert decision["rule_id"] == "output-secrets-v1"
    # Generation happened (the guard scans its output); the secret never ships.
    assert mock_bedrock.generate_calls, "the answer must be generated before it is scanned"
    assert "sk-ant-" not in json.dumps(body)


def test_pii_answer_on_query_returns_200_masked_with_a_transform_decision(
    mock_bedrock, mock_search, _cleanup_state
):
    mock_bedrock.generation_result = _gen(PII_ANSWER)
    client = _client(mock_bedrock, mock_search)

    body = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    ).json()

    text = body["result"]["answer"]["text"]
    assert "123-45-6789" not in text, "the SSN must be masked"
    assert "‹redacted:ssn›" in text
    # Citations are built over the REDACTED answer — the [chunk_id] markers survive.
    cited = {cid for c in body["result"]["answer"]["citations"] for cid in c["chunk_ids"]}
    assert cited == {f"{CANNED_DOC_ID}:0", f"{CANNED_DOC_ID}:1"}
    assert len(body["guardrail_decisions"]) == 1
    decision = body["guardrail_decisions"][0]
    assert decision["stage"] == "output"
    assert decision["decision"] == "transform"
    assert decision["category"] == "pii"
    assert decision["rule_id"] == "output-pii-redact-v1"
    assert "ssn=1" in decision["rationale"]


def test_email_answer_on_query_returns_200_unchanged_with_a_flag_decision(
    mock_bedrock, mock_search, _cleanup_state
):
    mock_bedrock.generation_result = _gen(FLAG_ANSWER)
    client = _client(mock_bedrock, mock_search)

    body = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    ).json()

    # Advisory only: the answer text is UNCHANGED (email is often legitimate).
    assert body["result"]["answer"]["text"] == FLAG_ANSWER
    assert len(body["guardrail_decisions"]) == 1
    decision = body["guardrail_decisions"][0]
    assert decision["decision"] == "flag"
    assert decision["category"] == "pii"
    assert decision["rule_id"] == "output-pii-flag-v1"


def test_clean_answer_on_query_has_empty_decisions_and_intact_citations(
    mock_bedrock, mock_search, _cleanup_state
):
    mock_bedrock.generation_result = _gen(CANNED_ANSWER_TEXT)
    client = _client(mock_bedrock, mock_search)

    body = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    ).json()

    assert body["result"]["answer"]["text"] == CANNED_ANSWER_TEXT
    assert body["guardrail_decisions"] == []
    cited = {cid for c in body["result"]["answer"]["citations"] for cid in c["chunk_ids"]}
    assert cited == {f"{CANNED_DOC_ID}:0", f"{CANNED_DOC_ID}:1"}


# --- Streaming POST /query/stream (buffered on 1.4.0) ----------------------


def test_secrets_answer_on_stream_emits_zero_tokens_and_one_final_refusal(
    mock_bedrock, mock_search, _cleanup_state
):
    mock_bedrock.generation_result = _gen(SECRETS_ANSWER)
    client = _client(mock_bedrock, mock_search)

    response = client.post(
        "/query/stream", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == ["final"], "buffered secrets block: ZERO token events, exactly one final"
    final = events[0][1]
    assert final["result"]["answer"]["text"] == REFUSAL_TEXT
    assert final["guardrail_decisions"][0]["decision"] == "block"
    assert final["guardrail_decisions"][0]["category"] == "secrets"
    assert "sk-ant-" not in response.text


def test_pii_answer_on_stream_emits_zero_tokens_and_one_masked_final(
    mock_bedrock, mock_search, _cleanup_state
):
    mock_bedrock.generation_result = _gen(PII_ANSWER)
    client = _client(mock_bedrock, mock_search)

    response = client.post(
        "/query/stream", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == ["final"], "buffered PII redact: ZERO token events, exactly one final"
    final = events[0][1]
    assert "123-45-6789" not in response.text
    assert "‹redacted:ssn›" in final["result"]["answer"]["text"]
    assert final["guardrail_decisions"][0]["decision"] == "transform"


def test_1_1_0_stream_still_live_streams_token_deltas_byte_for_byte(
    mock_bedrock, mock_search, _cleanup_state
):
    """1.1.0 has empty output_categories ⇒ the live token path is UNCHANGED."""
    client = _client(mock_bedrock, mock_search)

    response = client.post(
        "/query/stream",
        json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.1.0"},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    # token* then exactly one final — the pre-guard streaming contract, intact.
    assert names == ["token", "token", "token", "final"]
    streamed_deltas = [data["text"] for name, data in events if name == "token"]
    assert streamed_deltas == canned_stream_deltas(), "live deltas must be byte-for-byte identical"
    assert events[-1][1]["guardrail_decisions"] == []


# --- Contract + taxonomy invariants ---------------------------------------


def test_refusal_and_redacted_envelopes_validate_against_v1_1_0(
    mock_bedrock, mock_search, _cleanup_state
):
    # Secrets refusal envelope.
    mock_bedrock.generation_result = _gen(SECRETS_ANSWER)
    client = _client(mock_bedrock, mock_search)
    refusal = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    ).json()
    validate_result(refusal["result"])

    # Redacted (transform) envelope — a redacted answer is a normal answer.
    mock_bedrock.generation_result = _gen(PII_ANSWER)
    redacted = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
    ).json()
    validate_result(redacted["result"])


def test_output_secrets_tripwire_is_never_mapped_to_a_5xx(
    mock_bedrock, mock_search, _cleanup_state
):
    mock_bedrock.generation_result = _gen(SECRETS_ANSWER)
    client = _client(mock_bedrock, mock_search)

    for route in ("/query", "/query/stream"):
        response = client.post(
            route, json={"question": BENIGN_QUESTION, "pipeline_config": GUARD_CONFIG}
        )
        assert response.status_code == 200, route
        assert response.status_code not in (502, 503), route
