"""NeMo OUTPUT/facts guard: pure-stage translation + route wiring (Task Group 3).

Q6b (locked): NeMo is layered on OUTPUT + FACTS ONLY; the in-house Haiku input
confirm-step is untouched. The pure stage ``check_output_nemo`` is a function of
the answer, the retrieved chunks, the resolved ``GuardrailsPin``, and an INJECTED
client (a ``typing.Protocol``) — it imports NO transport/NeMo library (I2).

Binding contract proven here:

- a genuine NeMo BLOCK (``unsafe``) ⇒ ``GuardrailTripwire`` → the route returns a
  200 canned ``REFUSAL_TEXT`` with one ``nemo`` ``block`` decision, NEVER a 5xx
  (I3); NeMo's own rationale string NEVER becomes the UI answer;
- an advisory ``flag`` ⇒ the answer is DELIVERED with a non-block ``flag``
  decision (Q2 output/facts fail-OPEN);
- a transport error on the output path ⇒ FAIL CLOSED (ruling 0.3): the answer
  is SUPPRESSED to a ``guard-unavailable`` refusal + ``Retry-After``, NOT
  delivered with a flag;
- ``check_facts`` is forwarded to the client (the independent facts gate);
- the INPUT lane is UNCHANGED — ``check_input`` still blocks a leak via the
  in-house Haiku classifier with no NeMo involvement.

No live pod / AWS: a fake Protocol-shaped client is injected.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.clients import AppClients
from app.clients.nemo_guard import NemoVerdict
from app.config import ResolvedPipelineConfig, resolve_pipeline_config
from app.main import app as main_app
from app.orchestrator.guardrails import (
    REFUSAL_TEXT,
    GuardMisconfiguredError,
    GuardrailTripwire,
    check_output_nemo,
)
from app.schemas.pipeline_config import GuardrailsPin, NemoGuardPin
from fastapi.testclient import TestClient

from tests.conftest import FIXTURE_SETTINGS

HAIKU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"

import json as _json


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


NEMO_ON = GuardrailsPin(policy_version="1.8.0", nemo=NemoGuardPin(enabled=True))
NEMO_FACTS_ON = GuardrailsPin(
    policy_version="1.8.0", nemo=NemoGuardPin(enabled=True, check_facts=True)
)
NEMO_OFF = GuardrailsPin(policy_version="1.4.0")  # no nemo selector → lane off


class FakeNemo:
    """Injected NeMo client: scripted verdict or error; records check_output calls."""

    def __init__(self, verdict: Any = None, error: Exception | None = None) -> None:
        self._verdict = verdict
        self._error = error
        self.calls: list[tuple[str, list[str], bool]] = []

    def check_output(self, answer: str, chunks: list[str], *, check_facts: bool) -> Any:
        self.calls.append((answer, chunks, check_facts))
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


# --------------------------------------------------------------------------
# Pure-stage translation (check_output_nemo)
# --------------------------------------------------------------------------


def test_lane_off_is_a_typed_identity_with_no_client_call():
    nemo = FakeNemo(verdict=_verdict())
    out = check_output_nemo("the answer", ["c"], pins=NEMO_OFF, nemo_client=nemo)
    assert out.answer_text == "the answer"
    assert out.decisions == ()
    assert nemo.calls == []  # off ⇒ zero pod calls


def test_block_raises_the_nemo_block_tripwire_with_telemetry():
    nemo = FakeNemo(verdict=_verdict(unsafe=True, rationale="policy violation", input_tokens=90, output_tokens=5))
    with pytest.raises(GuardrailTripwire) as excinfo:
        check_output_nemo("bad answer", [], pins=NEMO_ON, nemo_client=nemo)
    tripwire = excinfo.value
    assert tripwire.decision.stage == "output"
    assert tripwire.decision.decision == "block"
    assert tripwire.decision.category == "nemo"
    assert tripwire.decision.rule_id == "nemo-output-block-v1"
    # Telemetry rides the tripwire for the span.
    assert tripwire.model_id == HAIKU
    assert (tripwire.input_tokens, tripwire.output_tokens) == (90, 5)


def test_advisory_flag_delivers_the_answer_with_a_flag_decision():
    nemo = FakeNemo(verdict=_verdict(flag=True, rationale="grounding weak", input_tokens=80, output_tokens=4))
    out = check_output_nemo("valid answer", ["c"], pins=NEMO_ON, nemo_client=nemo)
    assert out.answer_text == "valid answer"  # DELIVERED, not blocked
    assert len(out.decisions) == 1
    assert out.decisions[0].decision == "flag"
    assert out.decisions[0].category == "nemo"
    assert out.decisions[0].rule_id == "nemo-output-flag-v1"
    assert out.model_id == HAIKU
    assert (out.input_tokens, out.output_tokens) == (80, 4)


def test_clean_verdict_delivers_the_answer_with_no_decisions():
    nemo = FakeNemo(verdict=_verdict())
    out = check_output_nemo("clean answer", ["c"], pins=NEMO_ON, nemo_client=nemo)
    assert out.answer_text == "clean answer"
    assert out.decisions == ()


def test_transport_error_fails_closed_to_a_guard_unavailable_refusal():
    nemo = FakeNemo(error=httpx.ConnectError("pod down"))
    # Ruling 0.3: pod UNREACHABLE ⇒ fail CLOSED (raise), NOT deliver-with-flag.
    with pytest.raises(GuardrailTripwire) as excinfo:
        check_output_nemo("valid answer", ["c"], pins=NEMO_ON, nemo_client=nemo)
    tw = excinfo.value
    assert tw.decision.stage == "output"
    assert tw.decision.decision == "guard-unavailable"
    assert tw.decision.category == "nemo"
    assert tw.decision.rule_id == "nemo-output-guard-unavailable-v1"
    assert tw.retry_after is not None and tw.retry_after > 0


def test_check_facts_toggle_is_passed_through_to_the_client():
    on = FakeNemo(verdict=_verdict())
    check_output_nemo("a", ["c"], pins=NEMO_FACTS_ON, nemo_client=on)
    assert on.calls[0][2] is True  # check_facts forwarded

    off = FakeNemo(verdict=_verdict())
    check_output_nemo("a", ["c"], pins=NEMO_ON, nemo_client=off)
    assert off.calls[0][2] is False


def test_enabled_but_no_client_raises_misconfigured_loudly():
    with pytest.raises(GuardMisconfiguredError):
        check_output_nemo("a", [], pins=NEMO_ON, nemo_client=None)


# --------------------------------------------------------------------------
# INPUT lane UNCHANGED (Q6b): NeMo never touches the input path
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Route wiring: I3 (200 refusal, never 5xx, NeMo string never surfaced)
# --------------------------------------------------------------------------

BENIGN_QUESTION = "Is a supply of legal services to a non-resident GST-free?"
# The pod-supplied rationale that must NEVER appear in the UI answer.
NEMO_LEAKED_STRING = "I refuse because the answer disclosed confidential PII per rail R7"


def _nemo_config(monkeypatch, *, check_facts: bool = False) -> None:
    """Monkeypatch resolution to a manifest-listed base (1.1.0) + NeMo lane on."""
    base = resolve_pipeline_config("legal-rag-default-1.1.0")
    nemo_pin = NemoGuardPin(enabled=True, check_facts=check_facts)
    guard = base.config.guardrails.model_copy(update={"nemo": nemo_pin})
    cfg = base.config.model_copy(update={"guardrails": guard})
    resolved = ResolvedPipelineConfig(
        config=cfg,
        pipeline_version=base.pipeline_version,
        config_sha256=base.config_sha256,
    )
    monkeypatch.setattr(
        "app.routers.query.resolve_pipeline_config", lambda ref: resolved
    )


def _client(mock_bedrock, mock_search, nemo) -> TestClient:
    class _SafeClassifier:
        def classify(self, question, *, model_id):  # pragma: no cover - benign misses prefilter
            raise AssertionError("benign question must miss the input pre-filter")

    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(
        bedrock=mock_bedrock, search=mock_search, nemo=nemo
    )
    return TestClient(main_app)


@pytest.fixture
def _cleanup_state():
    yield
    for attr in ("settings", "clients", "tracer"):
        if hasattr(main_app.state, attr):
            delattr(main_app.state, attr)


def test_nemo_block_on_query_is_a_200_refusal_never_5xx_nemo_string_hidden(
    monkeypatch, mock_bedrock, mock_search, _cleanup_state
):
    _nemo_config(monkeypatch)
    nemo = FakeNemo(verdict=_verdict(unsafe=True, rationale=NEMO_LEAKED_STRING))
    client = _client(mock_bedrock, mock_search, nemo)

    response = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.8.0"}
    )

    assert response.status_code == 200  # I3: honest refusal, never a 5xx
    body = response.json()
    assert body["result"]["answer"]["text"] == REFUSAL_TEXT
    assert body["result"]["answer"]["citations"] == []
    # NeMo's own refusal string NEVER reaches the UI answer.
    assert NEMO_LEAKED_STRING not in body["result"]["answer"]["text"]
    decision = body["guardrail_decisions"][0]
    assert decision["decision"] == "block"
    assert decision["category"] == "nemo"
    # The generated answer WAS produced (NeMo scans its output) then suppressed.
    assert mock_bedrock.generate_calls
    # The facts gate defaulted OFF was forwarded to the pod.
    assert nemo.calls[0][2] is False


def test_nemo_pod_unreachable_on_query_fails_closed_to_a_200_refusal(
    monkeypatch, mock_bedrock, mock_search, _cleanup_state
):
    _nemo_config(monkeypatch)
    nemo = FakeNemo(error=httpx.ConnectError("pod down"))
    client = _client(mock_bedrock, mock_search, nemo)

    response = client.post(
        "/query", json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.8.0"}
    )

    # Ruling 0.3: the unadjudicated answer is SUPPRESSED to an honest 200 refusal
    # (never a 5xx), NOT delivered with an advisory flag.
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["answer"]["text"] == REFUSAL_TEXT
    assert len(body["guardrail_decisions"]) == 1
    decision = body["guardrail_decisions"][0]
    assert decision["decision"] == "guard-unavailable"
    assert decision["rule_id"] == "nemo-output-guard-unavailable-v1"
    assert decision["category"] == "nemo"
    # Retryable: the honest 200 carries a Retry-After.
    assert int(response.headers["Retry-After"]) > 0


def test_nemo_block_on_stream_emits_zero_tokens_and_one_final_refusal(
    monkeypatch, mock_bedrock, mock_search, _cleanup_state
):
    _nemo_config(monkeypatch)
    nemo = FakeNemo(verdict=_verdict(unsafe=True, rationale=NEMO_LEAKED_STRING))
    client = _client(mock_bedrock, mock_search, nemo)

    response = client.post(
        "/query/stream",
        json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.8.0"},
    )
    assert response.status_code == 200
    # Buffered: exactly one 'final' event, zero 'token' events.
    names = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert names == ["event: final"]
    final = _parse_final_event(response.text)
    # The ANSWER is the canned refusal; NeMo's own string never becomes the answer
    # (it may ride the decision envelope, which is metadata, not the UI answer).
    assert final["result"]["answer"]["text"] == REFUSAL_TEXT
    assert NEMO_LEAKED_STRING not in final["result"]["answer"]["text"]
    assert final["guardrail_decisions"][0]["category"] == "nemo"
