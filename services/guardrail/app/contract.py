"""
HTTP contract for the guardrail pod (request + PLAIN-DATA response).

The response maps 1:1 onto the orchestrator's existing ``ClassifierVerdict``
shape (``app/clients/guardrail.py``) so the orchestrator's ``NemoGuardClient``
(Phase 3) can translate a pod reply into the SAME plain data the pure stage
already consumes — the ``stages-pure`` seam barely changes. This is deliberately
just data: no NeMo / LangChain types cross the wire, so the orchestrator app
imports nothing new (I2).

Three request shapes, mirroring the rail stages:
- ``/check/input`` carries the user turn ONLY (self check input framing).
- ``/check/input/triage`` carries the user turn ONLY (Group 4 triage framing);
  it returns a three-way LABEL rather than a binary block.
- ``/check/output`` carries the generated ``answer`` PLUS the retrieved
  ``chunks`` (grounding evidence for self check facts) and an independent
  ``check_facts`` toggle (Q6) selecting whether the facts rail runs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CheckInputRequest(BaseModel):
    """``POST /check/input`` (and ``/check/input/triage``) body: the user turn only."""

    question: str = Field(
        description="The user turn to self-check (the orchestrator calls "
        "/check/input ONLY on a regex pre-filter HIT; /check/input/triage is "
        "called on EVERY question — Group 4's unconditional shadow triage).",
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

    Emitted by the pod's pure-regex secrets output rail (``app.detectors``).
    ``category`` is the CLASS that fired (``"secrets"`` is the only class the
    shipping detector table can produce), ``label`` the specific rule (e.g.
    ``"aws_access_key"``), ``count`` how many times it matched. Verdict-only,
    scoreable data (feeds Phase-2 scoring); the pod NEVER rewrites the answer, so
    offsets would be dead weight — deliberately omitted.

    The FIELDS are the frozen wire contract and do not change with the detector
    table: the withdrawal of the ``pii:`` table changed only what can populate
    them.
    """

    category: str = Field(
        description="The detector CLASS that fired (currently only 'secrets').",
    )
    label: str = Field(
        description="The specific rule label (e.g. 'aws_access_key').",
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


class ChunkInput(BaseModel):
    """One chunk a document was split into, sent for ingest-time injection scan."""

    id: str = Field(
        description="Stable chunk identifier (ingestion's chunk id) — echoed on "
        "the verdict so the caller and the UI can point at the offending part.",
    )
    text: str = Field(description="The chunk's extracted text (post-chunking).")
    ordinal: int | None = Field(
        default=None,
        description="0-based position of the chunk within the document, if known "
        "— forensic ordering only; the scan is independent of it.",
    )


class CheckChunksRequest(BaseModel):
    """
    ``POST /check/chunks`` body: a document's chunks, checked AFTER chunking.

    The ingest-time corpus-poisoning lane (Requirement 3). Ingestion calls this
    once per document, with the chunks it is about to embed + index; the pod
    returns a per-chunk safe/unsafe verdict. ANY unsafe chunk means the WHOLE
    document must be rejected (indexed: nothing) — the caller's policy, reported
    by ``CheckChunksResponse.safe``.
    """

    chunks: list[ChunkInput] = Field(
        description="The document's chunks (post-chunking, pre-index). An empty "
        "list is a vacuously-safe document (no content to poison).",
    )
    document_id: str | None = Field(
        default=None,
        description="Opaque document identifier, echoed on the response for the "
        "ingestion audit record and the frontend alert.",
    )
    source_ref: str | None = Field(
        default=None,
        description="Human-facing source reference (filename / URI), echoed for "
        "the 'document X is not safe to ingest' alert.",
    )


class ChunkDetection(BaseModel):
    """
    One injection hit inside a chunk — FORENSIC attribution (spans + excerpt).

    Emitted by the pod's pure-regex injection scanner (``app.chunk_scan``). Unlike
    :class:`Detection` (the answer-lane secrets attribution, which carries NO
    offsets because the pod never rewrites an answer), this lane DELIBERATELY
    carries the ``char_start``/``char_end`` span and an escaped ``matched_excerpt``
    / ``context``: the caller is an operator triaging an uploaded document and the
    whole point is to show *which part* is unsafe. Invisible/BIDI/zero-width
    codepoints are escaped to ``\\uXXXX`` so an unprintable hit is still legible.
    """

    category: str = Field(
        description="The injection class (prompt_injection / jailbreak / "
        "ai_directive / role_impersonation / bidi / invisible_unicode).",
    )
    label: str = Field(description="The specific rule label (e.g. 'ignore_previous').")
    severity: str = Field(
        description="Forensic ranking only ('high' | 'medium'); does NOT gate the "
        "verdict — any hit makes the chunk unsafe.",
    )
    char_start: int = Field(description="0-based start offset of the match in the chunk.")
    char_end: int = Field(description="Exclusive end offset of the match in the chunk.")
    matched_excerpt: str = Field(
        description="The exact matched span, with invisible/control codepoints "
        "escaped to \\uXXXX so it is legible in the alert.",
    )
    context: str = Field(
        description="A short surrounding window around the match (same escaping), "
        "so the operator sees the offending text in situ.",
    )


class ChunkVerdict(BaseModel):
    """Per-chunk verdict: safe/unsafe plus every forensic detection on that chunk."""

    chunk_id: str = Field(description="The input chunk's id, echoed back.")
    ordinal: int | None = Field(
        default=None, description="The input chunk's ordinal, echoed back if given."
    )
    verdict: Literal["safe", "unsafe"] = Field(
        description="'unsafe' when ANY injection rule fired on this chunk, else 'safe'.",
    )
    detections: list[ChunkDetection] = Field(
        default_factory=list,
        description="Every hit on this chunk (not deduplicated); empty when safe.",
    )


class CheckChunksResponse(BaseModel):
    """
    Whole-document verdict for ``POST /check/chunks`` (per-chunk, deterministic).

    ``safe`` is the load-bearing field: False when ANY chunk is unsafe, which the
    ingestion caller treats as "reject the whole document, index nothing, and
    alert the frontend with the forensic results". ``engine`` is ``deterministic``
    (no LLM call is made on this lane), so there is no ``model_id`` / token
    accounting — the verdict is a pure function of the chunks and the pinned
    ``injections.yml`` table.
    """

    safe: bool = Field(
        description="True only when EVERY chunk is safe. False → the caller must "
        "reject the whole document (index nothing) and alert.",
    )
    verdict: Literal["clean", "unsafe"] = Field(
        description="Whole-document roll-up: 'clean' iff safe, else 'unsafe'.",
    )
    document_id: str | None = Field(
        default=None, description="Echoed document identifier (audit + alert)."
    )
    source_ref: str | None = Field(
        default=None, description="Echoed source reference (filename / URI)."
    )
    chunk_count: int = Field(description="How many chunks were scanned.")
    unsafe_chunk_count: int = Field(description="How many chunks were unsafe.")
    detection_count: int = Field(
        description="Total injection hits across all chunks (for the alert roll-up).",
    )
    results: list[ChunkVerdict] = Field(
        description="Per-chunk verdicts in input order, each with its detections.",
    )
    engine: str = Field(
        default="deterministic",
        description="Always 'deterministic' — this lane makes NO LLM call, so the "
        "verdict survives a Bedrock outage at zero cost.",
    )


class TriageResponse(BaseModel):
    """
    PLAIN-DATA three-way verdict returned by ``POST /check/input/triage`` (Group 4).

    The triage rail is not a binary block: it returns a LABEL — ``attack`` (→ the
    orchestrator's block path), ``offtopic`` (→ redirect), or ``ok`` (→ allow) —
    recovered from the Colang flow's TWO distinct exception types (an ``ok`` turn
    raises none). ``unavailable`` marks an infrastructure failure (the pod could
    not adjudicate): the verdict then reads ``ok`` and the orchestrator applies
    its layered fail policy (ATTACK fails CLOSED, OFFTOPIC fails OPEN) at the
    boundary. NeMo's own refusal string is NEVER forwarded — only ``rationale``
    (the terse exception ``type``) may enter the decision/span.
    """

    verdict: Literal["attack", "offtopic", "ok"] = Field(
        description="The triage LABEL: 'attack' (block), 'offtopic' (redirect), "
        "or 'ok' (allow). Fails toward 'ok' on an unparseable/absent verdict.",
    )
    unavailable: bool = Field(
        default=False,
        description="True when the pod could NOT adjudicate (generate raised) — "
        "the verdict defaults to 'ok' and the orchestrator's layered fail policy "
        "decides (ATTACK closed, OFFTOPIC open). Distinct from a genuine 'ok'.",
    )
    rationale: str | None = Field(
        default=None,
        description="Terse reason (the exception `type` for a blocking label, or "
        "a fail label); NeMo's own refusal string is NEVER forwarded.",
    )
    input_tokens: int | None = Field(
        default=None, description="Bedrock prompt-token count for the triage call."
    )
    output_tokens: int | None = Field(
        default=None, description="Bedrock completion-token count for the triage call."
    )
    model_id: str = Field(
        description="The configured Haiku id, stamped by the pod (NOT read from "
        "NeMo, whose reported id is unreliable).",
    )
