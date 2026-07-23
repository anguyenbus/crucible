"""
The WRITER half of the fail-open alarm contract (item 9a, task 6.1).

The watch that alarms the fail-open window
(``services/eval/app/phoenix/fail_open_monitor.py``) reads Phoenix spans and
counts the ``guardrail.rule_id`` attribute. ``test_gate_mode_b.py`` already pins
that a Mode-B (pod unreachable) output lane delivers the answer with the LOUD
``nemo-output-fail-open-v1`` rule id in ``guardrail_decisions[]``. What nothing
asserted — and what the watch entirely depends on — is that the SAME id reaches
the ``guardrail_output`` SPAN, which is the only surface Phoenix (and therefore
the watch) can see. An envelope-only rule id would leave the monitor counting
zero through a real outage: a green watch over an unwatched window.

Failure injection is the EXISTING ``UnreachablePod`` from ``test_gate_mode_b``
(imported, not re-implemented — there is deliberately one injection mechanism).
No AWS, no live pod, no Phoenix server: the clients are mocked and the tracer is
an in-memory exporter.
"""

from __future__ import annotations

import pytest
from app.clients import AppClients
from app.main import app as main_app
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.conftest import FIXTURE_SETTINGS
from tests.test_gate_mode_b import (
    BENIGN_QUESTION,
    FAIL_OPEN_RULE_ID,
    ExplodingClassifier,
    UnreachablePod,
)

NEMO_ALL_CONFIG = "legal-rag-default-1.8.0"

# The span attribute the watch counts (`app.observability`), and the project the
# orchestrator's spans land in — the reader half is
# `app.phoenix.fail_open_monitor.RULE_ID_ATTRIBUTE`.
RULE_ID_ATTRIBUTE = "guardrail.rule_id"


@pytest.fixture
def _cleanup_state():
    yield
    for attr in ("settings", "clients", "tracer"):
        if hasattr(main_app.state, attr):
            delattr(main_app.state, attr)


def test_mode_b_fail_open_stamps_the_rule_id_on_the_guardrail_output_span(
    mock_bedrock, mock_search, _cleanup_state
):
    """
    A pod-unreachable window is COUNTABLE in Phoenix, not just in the envelope.

    Three benign answers are delivered while the pod is unreachable — the
    synthetic fail-open window the watch is built to see. Each must leave a
    ``guardrail_output`` span carrying ``guardrail.rule_id =
    nemo-output-fail-open-v1``; the watch's windowed count is exactly the number
    of such spans.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.tracer = provider.get_tracer("test-tracer")
    main_app.state.clients = AppClients(
        bedrock=mock_bedrock,
        search=mock_search,
        nemo=UnreachablePod(),
    )

    from fastapi.testclient import TestClient

    client = TestClient(main_app)
    responses = [
        client.post(
            "/query",
            json={"question": BENIGN_QUESTION, "pipeline_config": NEMO_ALL_CONFIG},
        )
        for _ in range(3)
    ]

    # Fail policy UNCHANGED: the answers are DELIVERED (200, never a 5xx, never
    # a refusal) — this group makes the window visible, it does not close it.
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert all(
        decision["rule_id"] == FAIL_OPEN_RULE_ID
        for response in responses
        for decision in response.json()["guardrail_decisions"]
    )

    fail_open_spans = [
        span
        for span in exporter.get_finished_spans()
        if span.name == "guardrail_output"
        and span.attributes.get(RULE_ID_ATTRIBUTE) == FAIL_OPEN_RULE_ID
    ]

    assert len(fail_open_spans) == 3, (
        "the fail-open rule id must reach the guardrail_output SPAN — it is the "
        "only surface the Phoenix-side watch can count"
    )
    attributes = fail_open_spans[0].attributes
    assert attributes["guardrail.stage"] == "output"
    assert attributes["guardrail.category"] == "nemo"
    # Advisory, never a block: the delivery happened and went out UNGUARDED.
    assert attributes["guardrail.decision"] == "flag"
