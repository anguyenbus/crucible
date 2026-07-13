"""
``POST /query`` response envelope and guardrail-decision models (Pydantic v2).

The response is an orchestrator-OWNED envelope wrapping eval's payload:

    {"result": <eval rag_query_output v1.1.0 payload>,
     "guardrail_decisions": [...],
     "generation_mode": "stub" | "live"}

The envelope top level is the ONLY home for orchestrator extensions, because
eval's payload declares ``additionalProperties: false`` — extensions inside
``result`` would break its schema. (``trace`` and ``timings_ms`` are NOT
extensions: eval's v1.1.0 schema declares them as optional slots inside the
payload.) Eval's HTTP adapter unwraps in one line: ``response["result"]``.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class GuardrailDecision(BaseModel):
    """
    One guardrail evaluation outcome.

    Mirrors the Bedrock Guardrails assessment shape — the likely Phase 3 wiring.

    ``stage`` and ``decision`` are EXTENSIBLE open vocabularies: plain strings
    with documented value sets, deliberately NOT closed ``Literal``/``Enum``
    types, so adding values later is non-breaking and does not fight the
    oasdiff breaking-change gate.
    """

    stage: str = Field(
        description=(
            "Pipeline stage the guardrail ran at. Documented vocabulary: "
            "'input' | 'output'. EXTENSIBLE — additional values may be added "
            "in later phases without a breaking change."
        ),
    )
    decision: str = Field(
        description=(
            "Guardrail outcome. Documented vocabulary: 'allow' | 'block' | "
            "'flag' | 'transform'. EXTENSIBLE — additional values may be added "
            "in later phases without a breaking change."
        ),
    )
    rule_id: str | None = Field(
        default=None,
        description="Identifier of the guardrail rule/policy element that fired.",
    )
    category: str | None = Field(
        default=None,
        description="Guardrail category (e.g. denied topic, PII, content filter).",
    )
    rationale: str | None = Field(
        default=None,
        description="Human-readable explanation of why the decision was taken.",
    )


class QueryResponse(BaseModel):
    """
    Orchestrator-owned ``/query`` response envelope.

    ``result`` is deliberately an OPAQUE ``dict[str, Any]`` passed through
    verbatim — it is NEVER re-modeled as nested Pydantic models, which could
    strip or inject keys and neuter the eval schema's ``additionalProperties:
    false`` drift detection. Conformance of ``result`` is proven by jsonschema
    validation (Draft 2020-12) against the packaged
    ``rag_query_output.schema.json``, not by Pydantic.

    Orchestrator extensions live ONLY at this envelope's top level (eval's
    payload is ``additionalProperties: false``): currently the
    ``generation_mode`` marker and ``guardrail_decisions``.
    """

    result: dict[str, Any] = Field(
        description=(
            "Eval rag_query_output v1.1.0 payload, passed through VERBATIM. "
            "Validates against the packaged rag_query_output.schema.json as-is "
            "(no key stripping anywhere). Includes the declared optional "
            "trace and timings_ms slots (trace only when Phoenix tracing is "
            "configured)."
        ),
    )
    guardrail_decisions: list[GuardrailDecision] = Field(
        description=(
            "REQUIRED list of guardrail outcomes. Empty in every Phase 2 "
            "response; populated when Bedrock Guardrails are wired in Phase 3."
        ),
    )
    generation_mode: Literal["stub", "live"] = Field(
        description=(
            "Machine-readable generation-mode marker: always 'live' from "
            "Phase 2 (real retrieval + generation). 'stub' survives only as "
            "an enum value for contract stability — no reachable code path "
            "produces it."
        ),
    )
