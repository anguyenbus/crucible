"""
Skeleton + contract tests for the guardrail pod (LLMRails MOCKED — no AWS).

Covers task 1.1: /healthz liveness (+ /readyz reflecting engine construction),
the two check endpoints accepting their request contract and returning the
plain-data response shape, the input=user-turn-only / output=answer+chunks
asymmetry, and the exception-turn → unsafe mapping (FINDINGS #5).
"""

from __future__ import annotations

from tests.helpers import FakeRails, benign_res, block_res

_CONTRACT_FIELDS = {
    "unsafe",
    "rationale",
    "input_tokens",
    "output_tokens",
    "model_id",
    "flag",
    "detections",
}
_HAIKU_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_healthz_ok_and_readyz_reflects_engine(make_client):
    """/healthz is always 200; /readyz is 200 with an engine, 503 without."""
    ready_client = make_client(FakeRails(benign_res()))
    assert ready_client.get("/healthz").json() == {"status": "ok"}
    assert ready_client.get("/healthz").status_code == 200

    # Engine present → ready.
    ready = ready_client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}

    # Engine absent (rails=None) → not ready, honest 503 (never a 5xx stack).
    not_ready_client = make_client(None)
    resp = not_ready_client.get("/readyz")
    assert resp.status_code == 503
    assert "not initialized" in resp.json()["detail"]


def test_check_input_accepts_user_turn_and_returns_plain_data(make_client):
    """/check/input: benign turn → full contract shape, tokens parsed, id stamped."""
    rails = FakeRails(benign_res())
    client = make_client(rails)

    resp = client.post("/check/input", json={"question": "What is a lease?"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _CONTRACT_FIELDS
    assert body["unsafe"] is False
    assert body["flag"] is False
    # The input lane carries NO deterministic detections (output-rail only).
    assert body["detections"] == []
    # Tokens come from the logged Bedrock call (FINDINGS #3).
    assert body["input_tokens"] == 170
    assert body["output_tokens"] == 114
    # model_id is STAMPED by the pod from settings, NOT read from NeMo (FINDINGS #2).
    assert body["model_id"] == _HAIKU_ID
    # Only the user turn was sent to the rail.
    assert rails.calls[0]["messages"] == [{"role": "user", "content": "What is a lease?"}]


def test_check_output_accepts_answer_and_chunks_and_maps_block(make_client):
    """/check/output: takes answer+chunks; a rail exception turn → unsafe=True."""
    rails = FakeRails(block_res("OutputRailException"))
    client = make_client(rails)

    resp = client.post(
        "/check/output",
        json={"answer": "The tenant may terminate anytime.", "chunks": ["30 days notice."]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _CONTRACT_FIELDS
    # Exception-role turn is the unsafe signal; rationale is the terse type label,
    # never NeMo's own prose.
    assert body["unsafe"] is True
    assert body["rationale"] == "OutputRailException"
    assert body["model_id"] == _HAIKU_ID
    # The grounding chunks reached the rail invocation.
    sent = rails.calls[0]["messages"]
    assert any(
        m.get("role") == "context" and m["content"].get("relevant_chunks") == ["30 days notice."]
        for m in sent
    )


def test_request_contract_asymmetry_and_validation(make_client):
    """Input carries the user turn ONLY; output requires the answer (+ optional chunks)."""
    client = make_client(FakeRails(benign_res()))

    # /check/input requires `question`.
    assert client.post("/check/input", json={}).status_code == 422
    # /check/output requires `answer`; chunks defaults to [] (optional).
    assert client.post("/check/output", json={"chunks": ["c"]}).status_code == 422
    ok = client.post("/check/output", json={"answer": "grounded answer"})
    assert ok.status_code == 200
    assert ok.json()["model_id"] == _HAIKU_ID
