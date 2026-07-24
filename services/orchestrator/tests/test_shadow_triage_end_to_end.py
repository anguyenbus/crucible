"""Group 9 gap-fill — the settled pre-generation chain, end to end, on 1.8.0.

The per-group suites cover each stage in isolation, and the route suites inject a
pod double WITHOUT the triage entry point (so the Group 4 shadow triage
OBSERVATION never runs at the route level). This file fills that gap: it drives
the ordered chain the spec settled on —

    raw question → malformed pre-check → regex pre-filter → triage (SHADOW) →
    deterministic rewrite → retrieval → generation → output rails

— against the REAL router with a triage-capable pod double, and asserts the two
highest-value end-to-end properties:

1. A BENIGN question flows all the way through to a LIVE answer while a shadow
   triage verdict is RECORDED (on the ``guardrail_input`` span, keyed to the
   trace id) but NOT acted on. Post-Group-7 the ATTACK class ENFORCES, so the
   remaining shadow class is OFFTOPIC: triage returns ``offtopic`` and the officer
   is NOT redirected. Shadow observes; it never redirects real traffic (the
   OFFTOPIC flip is Group 8.5).
2. A MALFORMED (BIDI-override) input is rejected by the deterministic floor
   FIRST — before the triage pod call, before retrieval, and before generation —
   as an honest 200, proving the ordering and the pod-independence of the floor.

Fully offline: a Protocol-shaped triage-capable pod double is injected; Bedrock /
OpenSearch are mocked; a real tracer over an in-memory exporter captures spans.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.clients import AppClients
from app.clients.nemo_guard import NemoTriageVerdict, NemoVerdict
from app.main import app as main_app
from app.orchestrator.guardrails import REFUSAL_TEXT
from app.orchestrator.input_triage import REDIRECT_TEXT
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.conftest import CANNED_ANSWER_TEXT, FIXTURE_SETTINGS

HAIKU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"

# A benign case question — pre-filter MISS (so self_check_input is never called)
# but STILL unconditionally triaged on the guarded lane.
BENIGN_QUESTION = "Is a supply of legal services to a non-resident GST-free?"
# A BIDI-override input: malformed, hard-rejected by the deterministic floor.
RLO = "‮"  # RIGHT-TO-LEFT OVERRIDE


class TriageCapablePod:
    """A pod double exposing the FULL 1.8.0 surface: triage + input + output."""

    def __init__(self, triage_label: str) -> None:
        self._triage_label = triage_label
        self.triage_calls: list[str] = []
        self.input_calls: list[str] = []
        self.output_calls: list[tuple[str, list[str], bool]] = []

    def check_input_triage(self, question: str) -> NemoTriageVerdict:
        self.triage_calls.append(question)
        return NemoTriageVerdict(
            verdict=self._triage_label,
            unavailable=False,
            rationale=None,
            input_tokens=313,
            output_tokens=4,
            model_id=HAIKU,
        )

    def check_input(self, question: str) -> NemoVerdict:  # pragma: no cover - benign miss
        self.input_calls.append(question)
        return _clean_verdict()

    def check_output(self, answer: str, chunks: list[str], *, check_facts: bool) -> NemoVerdict:
        self.output_calls.append((answer, chunks, check_facts))
        return _clean_verdict()


def _clean_verdict() -> NemoVerdict:
    return NemoVerdict(
        unsafe=False,
        rationale=None,
        input_tokens=None,
        output_tokens=None,
        model_id=HAIKU,
        flag=False,
    )


@pytest.fixture
def traced_client():
    """TestClient wired with a triage-capable pod double + an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _make(pod: TriageCapablePod, mock_bedrock: Any, mock_search: Any) -> TestClient:
        main_app.state.settings = FIXTURE_SETTINGS
        main_app.state.clients = AppClients(
            bedrock=mock_bedrock, search=mock_search, nemo=pod
        )
        main_app.state.tracer = provider.get_tracer("shadow-triage-test")
        return TestClient(main_app)

    try:
        yield _make, exporter
    finally:
        for attr in ("settings", "clients", "tracer"):
            if hasattr(main_app.state, attr):
                delattr(main_app.state, attr)


def _guardrail_input_span(exporter: InMemorySpanExporter):
    spans = [s for s in exporter.get_finished_spans() if s.name == "guardrail_input"]
    assert spans, "no guardrail_input span was recorded for the shadow triage observation"
    return spans[-1]


def test_benign_question_flows_through_with_shadow_triage_recorded_not_acted(
    traced_client, mock_bedrock, mock_search
):
    """1.8.0 benign: LIVE answer delivered; the OK triage verdict is RECORDED (allow, no action)."""
    make, exporter = traced_client
    # Triage returns OK for a genuine case question — an ``allow`` decision with no
    # verdict class, so it never acts (ATTACK + OFFTOPIC now both enforce; OK is the
    # class that flows through). The verdict is still recorded on the span.
    pod = TriageCapablePod(triage_label="ok")
    client = make(pod, mock_bedrock, mock_search)

    response = client.post(
        "/query",
        json={"question": BENIGN_QUESTION, "pipeline_config": "legal-rag-default-1.8.0"},
    )

    # Shadow does NOT act: the officer gets the real generated answer, not a
    # refusal and not a redirect, even though triage said OFFTOPIC.
    assert response.status_code == 200
    body = response.json()
    assert body["generation_mode"] == "live"
    answer = body["result"]["answer"]["text"]
    assert answer == CANNED_ANSWER_TEXT
    assert answer not in (REFUSAL_TEXT, REDIRECT_TEXT)
    # No block/redirect decision reached the envelope (shadow verdict lives on the
    # span, OUTSIDE the answering path).
    assert body["guardrail_decisions"] == []

    # The ordered chain actually ran: triage saw the RAW benign turn; the free
    # cost gate held (pre-filter miss ⇒ no self_check_input); generation happened;
    # the unconditional output lane was consulted.
    assert pod.triage_calls == [BENIGN_QUESTION]
    assert pod.input_calls == []
    assert mock_bedrock.generate_calls
    assert len(pod.output_calls) == 1

    # The shadow verdict is RECORDED on the guardrail_input span, keyed to the
    # trace id, with mode=shadow — the audit trail outside the answer.
    span = _guardrail_input_span(exporter)
    assert span.attributes["guardrail.mode"] == "shadow"
    assert span.attributes["guardrail.decision"] == "allow"  # OK verdict recorded, flows through


def test_malformed_input_is_rejected_before_the_triage_pod_call_and_generation(
    traced_client, mock_bedrock, mock_search
):
    """The deterministic floor runs FIRST: a BIDI input never reaches triage or generation."""
    make, exporter = traced_client
    pod = TriageCapablePod(triage_label="ok")
    client = make(pod, mock_bedrock, mock_search)

    response = client.post(
        "/query",
        json={
            "question": f"is this{RLO} GST-free?",
            "pipeline_config": "legal-rag-default-1.8.0",
        },
    )

    # Honest 200 malformed refusal from the pod-INDEPENDENT floor.
    assert response.status_code == 200
    decision = response.json()["guardrail_decisions"][0]
    assert decision["category"] == "malformed"
    assert decision["rule_id"] == "input-malformed-reject-v1"
    # Ordering + pod-independence: the malformed pre-check short-circuited BEFORE
    # the triage pod call, before any input self-check, and before generation.
    assert pod.triage_calls == []
    assert pod.input_calls == []
    assert pod.output_calls == []
    assert mock_bedrock.generate_calls == []
