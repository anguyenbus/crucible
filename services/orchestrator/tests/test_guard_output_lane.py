"""
Group 5 — the ratified case-officer OUTPUT lane: PII-visible, unconditional,
buffered, verdict-only, and (ruling 0.3) FAIL-CLOSED on pod-unreachable.

These are the 2-8 focused tests written FIRST (task 5.1, TDD). They pin the
genuinely-new / verify surface of Group 5:

- 5.2 PII-allow carve-out: a realistic case-officer answer carrying an ABN + BSB
  + a deposit figure is DELIVERED VERBATIM — never blocked, never redacted. PII
  is VISIBLE to the cleared officer (the deterministic PII table stays WITHDRAWN;
  the ``self check output`` rail allows identifiers/figures).
- 5.3 unconditional: on the guarded 1.8.0 lane the output guard runs on EVERY
  answer (a benign answer still reaches the pod); off (no ``nemo`` selector) it
  is a typed identity.
- 5.4 buffered ``/query/stream``: when the output guard is active, ZERO token
  events are emitted and the full answer is delivered in exactly ONE ``final``
  event (buffer-then-deliver — no simulated/typewriter tokens, the orchestrator's
  honesty constraint).
- 5.5 verdict-only: NeMo's own refusal string NEVER surfaces — only the terse
  rationale enters the decision; the pod never rewrites the answer.
- 5.6 (ruling 0.3): pod UNREACHABLE on the output lane fails CLOSED to an honest
  200 refusal carrying ``nemo-output-guard-unavailable-v1`` + ``Retry-After`` —
  NOT deliver-with-flag.

Fully offline: a Protocol-shaped fake pod client is injected (``stages-pure`` —
no transport import in the stage); Bedrock/OpenSearch are mocked. No AWS, no
live pod.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest
from app.clients import AppClients
from app.clients.bedrock import GenerationResult
from app.clients.nemo_guard import NemoVerdict
from app.config import ResolvedPipelineConfig, resolve_pipeline_config
from app.main import app as main_app
from app.orchestrator.guardrails import (
    REFUSAL_TEXT,
    GuardrailTripwire,
    check_output_nemo,
    nemo_output_active,
)
from app.schemas.pipeline_config import GuardrailsPin, NemoGuardPin
from fastapi.testclient import TestClient

from tests.conftest import FIXTURE_SETTINGS

HAIKU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"

# The kind of answer this product EXISTS to produce: a case officer's compliance
# finding that quotes the subject's own financial identifiers and figures. Every
# value is a standard fictitious / published test value, never real data.
CASE_OFFICER_ANSWER = (
    "The subject entity trades under ABN 51 824 753 556. Remittances were paid "
    "to BSB 062-000 account 12345678, and a deposit of $4,250,000.00 was "
    "identified that is not reflected in the declared income for the period."
)

NEMO_ON = GuardrailsPin(policy_version="1.8.0", nemo=NemoGuardPin(enabled=True))
NEMO_OFF = GuardrailsPin(policy_version="1.4.0")  # no nemo selector → lane off


class FakeNemo:
    """Injected pod client: scripted output verdict or transport error; records calls."""

    def __init__(self, verdict: Any = None, error: Exception | None = None) -> None:
        self._verdict = verdict
        self._error = error
        self.output_calls: list[tuple[str, list[str], bool]] = []

    def check_output(self, answer: str, chunks: list[str], *, check_facts: bool) -> Any:
        self.output_calls.append((answer, chunks, check_facts))
        if self._error is not None:
            raise self._error
        return self._verdict


def _verdict(**kw: Any) -> NemoVerdict:
    base = {
        "unsafe": False,
        "rationale": None,
        "input_tokens": None,
        "output_tokens": None,
        "model_id": HAIKU,
        "flag": False,
    }
    base.update(kw)
    return NemoVerdict(**base)


def _parse_final_event(body: str) -> dict:
    """Extract the single SSE ``final`` event payload from a stream body."""
    for block in body.split("\n\n"):
        if "event: final" in block:
            data = "".join(
                line.removeprefix("data: ")
                for line in block.split("\n")
                if line.startswith("data: ")
            )
            return _json.loads(data)
    raise AssertionError("no final event in stream")


# --------------------------------------------------------------------------
# 5.2 / 5.3 — pure-stage: PII visible, unconditional, verdict-only
# --------------------------------------------------------------------------


def test_pii_allow_carveout_case_officer_answer_delivered_verbatim():
    """5.2: an ABN + BSB + deposit answer PASSES — never blocked, never redacted."""
    nemo = FakeNemo(verdict=_verdict())
    out = check_output_nemo(CASE_OFFICER_ANSWER, ["chunk"], pins=NEMO_ON, nemo_client=nemo)
    # Delivered byte-for-byte: the cleared officer sees every identifier/figure.
    assert out.answer_text == CASE_OFFICER_ANSWER
    assert "ABN 51 824 753 556" in out.answer_text
    assert "BSB 062-000 account 12345678" in out.answer_text
    assert "$4,250,000.00" in out.answer_text
    assert out.decisions == ()  # no block, no redaction, no flag


def test_output_lane_is_unconditional_on_the_guarded_default():
    """5.3: on 1.8.0 the output guard runs on EVERY answer; off configs are identity."""
    assert nemo_output_active(NEMO_ON) is True
    assert nemo_output_active(NEMO_OFF) is False

    on = FakeNemo(verdict=_verdict())
    check_output_nemo("a benign case finding", ["c"], pins=NEMO_ON, nemo_client=on)
    assert len(on.output_calls) == 1  # unconditional: the benign answer is checked

    off = FakeNemo(verdict=_verdict())
    out = check_output_nemo("a benign case finding", ["c"], pins=NEMO_OFF, nemo_client=off)
    assert off.output_calls == []  # off ⇒ zero pod calls (typed identity)
    assert out.answer_text == "a benign case finding"


def test_pod_unreachable_output_fails_closed_to_guard_unavailable_with_retry_after():
    """5.6 (ruling 0.3): pod UNREACHABLE ⇒ fail CLOSED, NOT deliver-with-flag."""
    nemo = FakeNemo(error=httpx.ConnectError("pod down"))
    with pytest.raises(GuardrailTripwire) as excinfo:
        check_output_nemo(CASE_OFFICER_ANSWER, ["c"], pins=NEMO_ON, nemo_client=nemo)
    tw = excinfo.value
    assert tw.decision.stage == "output"
    assert tw.decision.decision == "guard-unavailable"
    assert tw.decision.category == "nemo"
    assert tw.decision.rule_id == "nemo-output-guard-unavailable-v1"
    # Retryable: a transient pod outage clears fast (consistent with the input lane).
    assert tw.retry_after is not None and tw.retry_after > 0


# --------------------------------------------------------------------------
# 5.2 / 5.4 / 5.5 / 5.6 — full route wiring (200 refusal never 5xx)
# --------------------------------------------------------------------------

BENIGN_QUESTION = "Is a supply of legal services to a non-resident GST-free?"
NEMO_LEAKED_STRING = "I refuse because the answer disclosed confidential PII per rail R7"


def _nemo_config(monkeypatch) -> None:
    """Resolve a manifest-listed base (1.1.0) with the NeMo output lane on (1.8.0-shaped)."""
    base = resolve_pipeline_config("legal-rag-default-1.1.0")
    guard = base.config.guardrails.model_copy(update={"nemo": NemoGuardPin(enabled=True)})
    cfg = base.config.model_copy(update={"guardrails": guard})
    resolved = ResolvedPipelineConfig(
        config=cfg,
        pipeline_version=base.pipeline_version,
        config_sha256=base.config_sha256,
    )
    monkeypatch.setattr("app.routers.query.resolve_pipeline_config", lambda ref: resolved)


def _client(mock_bedrock, mock_search, nemo) -> TestClient:
    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(bedrock=mock_bedrock, search=mock_search, nemo=nemo)
    return TestClient(main_app)


@pytest.fixture
def _cleanup_state():
    yield
    for attr in ("settings", "clients", "tracer"):
        if hasattr(main_app.state, attr):
            delattr(main_app.state, attr)


def test_pod_unreachable_output_on_query_is_a_200_refusal_with_retry_after_header(
    monkeypatch, mock_bedrock, mock_search, _cleanup_state
):
    """5.6: pod-unreachable output ⇒ honest 200 refusal + Retry-After, never a 5xx."""
    _nemo_config(monkeypatch)
    nemo = FakeNemo(error=httpx.ConnectError("pod down"))
    client = _client(mock_bedrock, mock_search, nemo)

    response = client.post(
        "/query",
        json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.8.0"},
    )

    assert response.status_code == 200  # fail-closed is an honest refusal, never 5xx
    body = response.json()
    # The answer is SUPPRESSED to the canned refusal (not delivered-with-flag).
    assert body["result"]["answer"]["text"] == REFUSAL_TEXT
    decision = body["guardrail_decisions"][0]
    assert decision["decision"] == "guard-unavailable"
    assert decision["rule_id"] == "nemo-output-guard-unavailable-v1"
    assert decision["stage"] == "output"
    # The refusal is retryable — the Retry-After header rides the honest 200.
    assert int(response.headers["Retry-After"]) > 0
    # The answer WAS generated (then suppressed) and the pod WAS attempted.
    assert mock_bedrock.generate_calls
    assert len(nemo.output_calls) == 1


def test_verdict_only_nemo_refusal_string_never_surfaces_on_a_block(
    monkeypatch, mock_bedrock, mock_search, _cleanup_state
):
    """5.5: a genuine block ⇒ REFUSAL_TEXT; NeMo's own prose NEVER becomes the answer."""
    _nemo_config(monkeypatch)
    nemo = FakeNemo(verdict=_verdict(unsafe=True, rationale=NEMO_LEAKED_STRING))
    client = _client(mock_bedrock, mock_search, nemo)

    body = client.post(
        "/query",
        json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.8.0"},
    ).json()

    assert body["result"]["answer"]["text"] == REFUSAL_TEXT
    assert NEMO_LEAKED_STRING not in body["result"]["answer"]["text"]
    decision = body["guardrail_decisions"][0]
    assert decision["decision"] == "block"
    assert decision["category"] == "nemo"


def test_buffered_stream_delivers_pii_answer_in_one_final_zero_tokens(
    monkeypatch, mock_bedrock, mock_search, _cleanup_state
):
    """5.4: an active output guard buffers the stream — zero tokens, one final, PII intact."""
    _nemo_config(monkeypatch)
    # The generated answer carries the officer's financial identifiers + figure.
    mock_bedrock.generation_result = GenerationResult(
        text=CASE_OFFICER_ANSWER,
        model_id="au.anthropic.claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=20,
        stop_reason="end_turn",
    )
    mock_bedrock.stream_deltas = [CASE_OFFICER_ANSWER]
    nemo = FakeNemo(verdict=_verdict())
    client = _client(mock_bedrock, mock_search, nemo)

    response = client.post(
        "/query/stream",
        json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.8.0"},
    )
    assert response.status_code == 200
    # Buffered: exactly one 'final', ZERO 'token' events (no simulated typewriter).
    names = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert names == ["event: final"]
    final = _parse_final_event(response.text)
    # PII VISIBLE: the deposit figure + identifiers survive the buffered delivery.
    answer = final["result"]["answer"]["text"]
    assert "$4,250,000.00" in answer
    assert "ABN 51 824 753 556" in answer
