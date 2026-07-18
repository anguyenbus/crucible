"""
HTTP contract for the guardrail pod (request + PLAIN-DATA response).

The response maps 1:1 onto the orchestrator's existing ``ClassifierVerdict``
shape (``app/clients/guardrail.py``) so the orchestrator's ``NemoGuardClient``
(Phase 3) can translate a pod reply into the SAME plain data the pure stage
already consumes — the ``stages-pure`` seam barely changes. This is deliberately
just data: no NeMo / LangChain types cross the wire, so the orchestrator app
imports nothing new (I2).

Two request shapes, mirroring the two rail stages:
- ``/check/input`` carries the user turn ONLY (self check input framing).
- ``/check/output`` carries the generated ``answer`` PLUS the retrieved
  ``chunks`` (grounding evidence for self check facts) and an independent
  ``check_facts`` toggle (Q6) selecting whether the facts rail runs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckInputRequest(BaseModel):
    """``POST /check/input`` body: the user turn only."""

    question: str = Field(
        description="The user turn to self-check (the orchestrator calls this "
        "ONLY on a regex pre-filter HIT, so benign traffic never reaches it).",
    )


class CheckOutputRequest(BaseModel):
    """``POST /check/output`` body: the generated answer plus grounding chunks."""

    answer: str = Field(description="The generated answer to self-check.")
    chunks: list[str] = Field(
        default_factory=list,
        description="Retrieved chunk texts (grounding evidence for self check "
        "facts); empty list is allowed when facts grounding is not engaged.",
    )
    check_facts: bool = Field(
        default=False,
        description="Independent facts gate (Q6): when True the pod runs the "
        "`self check facts` grounding rail over `answer` vs `chunks` IN ADDITION "
        "to `self check output`; when False the facts rail makes ZERO LLM calls. "
        "This is the toggle the orchestrator's independently-gated facts category "
        "maps to, so grounding is A/B-activatable separately from output "
        "self-check (default OFF — facts is the highest-value / highest-FP-risk "
        "rail).",
    )


class Detection(BaseModel):
    """
    One deterministic detector hit: labels + COUNTS only, NO spans/offsets.

    Emitted by the pod's pure-regex secrets/PII output rail
    (``app.detectors``). ``category`` is the CLASS that fired (``"secrets"`` /
    ``"pii"``), ``label`` the specific rule (e.g. ``"email"``, ``"credit_card"``,
    ``"aws_access_key"``), ``count`` how many times it matched (e.g. ``email=2``).
    Verdict-only, scoreable data (feeds Phase-2 scoring); the pod NEVER rewrites
    the answer, so offsets would be dead weight — deliberately omitted.
    """

    category: str = Field(
        description="The detector CLASS that fired: 'secrets' or 'pii'.",
    )
    label: str = Field(
        description="The specific rule label (e.g. 'email', 'credit_card', "
        "'aws_access_key').",
    )
    count: int = Field(
        description="How many times the rule matched the answer text.",
    )


class CheckResponse(BaseModel):
    """
    PLAIN-DATA verdict returned by both check endpoints.

    Maps 1:1 onto the orchestrator's ``ClassifierVerdict`` fields plus an
    advisory ``flag`` for the fail-OPEN output/facts path (Q2): a set ``flag``
    means "deliver the answer but attach an advisory GuardrailDecision", NEVER a
    block. ``model_id`` is STAMPED by the pod from its configured Haiku id —
    NeMo's reported id is unreliable (FINDINGS #2), so it is never read back from
    NeMo.
    """

    unsafe: bool = Field(
        description="True when a rail blocked (surfaced as a role:'exception' "
        "turn) OR the input lane failed SAFE; the orchestrator translates this "
        "into its honest 200 refusal.",
    )
    rationale: str | None = Field(
        default=None,
        description="Short reason from the rail exception content (or a terse "
        "fail-policy label); NeMo's own refusal string is NEVER forwarded to the "
        "UI.",
    )
    input_tokens: int | None = Field(
        default=None, description="Bedrock prompt-token count for the guard call."
    )
    output_tokens: int | None = Field(
        default=None, description="Bedrock completion-token count for the guard call."
    )
    model_id: str = Field(
        description="The configured Haiku id, stamped by the pod (NOT read from "
        "NeMo, whose reported id is unreliable).",
    )
    flag: bool = Field(
        default=False,
        description="Advisory fail-OPEN signal for the output/facts path "
        "(deliver-with-flag); never itself a block.",
    )
    detections: list[Detection] = Field(
        default_factory=list,
        description="Deterministic secrets/PII detector attribution (labels + "
        "counts, NO offsets) from the pod's FIRST output rail. Recorded even when "
        "a block short-circuits the paid LLM rails; empty on the input lane and "
        "on a clean output.",
    )
