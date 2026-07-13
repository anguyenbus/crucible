"""Contract-layer model tests: QueryRequest, envelope, GuardrailDecision.

Focused checks only (per task 2.1) — exhaustive validation permutations are
intentionally skipped.
"""

import pytest
from app.schemas.envelope import GuardrailDecision, QueryResponse
from app.schemas.query import QueryRequest
from pydantic import ValidationError


def test_query_request_accepts_valid_payload_and_rejects_malformed_ref():
    """A fully-qualified {name}-{semver} ref passes; a bare name fails (→ 422)."""
    request = QueryRequest(
        question="What is the GST treatment?",
        query_id="q-001",
        pipeline_config="legal-rag-default-1.0.0",
        metadata={"category": "gst"},
    )
    assert request.pipeline_config == "legal-rag-default-1.0.0"

    with pytest.raises(ValidationError):
        QueryRequest(question="q", pipeline_config="not_a_ref")


def test_query_request_rejects_unknown_fields():
    """Extra fields are forbidden: no top_k, no session/history fields sneak in."""
    with pytest.raises(ValidationError):
        QueryRequest(
            question="q",
            pipeline_config="legal-rag-default-1.0.0",
            top_k=5,
        )
    with pytest.raises(ValidationError):
        QueryRequest(
            question="q",
            pipeline_config="legal-rag-default-1.0.0",
            session_id="s-1",
        )


def test_envelope_requires_guardrail_decisions_and_generation_mode():
    """`guardrail_decisions` and `generation_mode` are REQUIRED envelope fields."""
    envelope = QueryResponse(
        result={"schema_version": "1.1.0"},
        guardrail_decisions=[],
        generation_mode="stub",
    )
    assert envelope.guardrail_decisions == []
    assert envelope.generation_mode == "stub"
    # result stays an opaque dict — never re-modeled, never mutated.
    assert envelope.result == {"schema_version": "1.1.0"}

    with pytest.raises(ValidationError):
        QueryResponse(result={}, generation_mode="stub")  # missing guardrail_decisions
    with pytest.raises(ValidationError):
        QueryResponse(result={}, guardrail_decisions=[])  # missing generation_mode


def test_guardrail_decision_accepts_documented_and_extension_values():
    """stage/decision carry documented vocabularies but stay EXTENSIBLE plain str."""
    decision = GuardrailDecision(
        stage="input",
        decision="block",
        rule_id="denied-topics/legal-advice",
        category="denied_topics",
        rationale="Query matched a denied topic.",
    )
    assert decision.stage == "input"
    assert decision.decision == "block"

    # Extensibility: undocumented values must NOT be rejected (open vocabulary,
    # so Phase 3 additions are non-breaking and do not fight the oasdiff gate).
    extension = GuardrailDecision(stage="retrieval", decision="redact")
    assert extension.stage == "retrieval"
    assert extension.decision == "redact"
