"""
Envelope → render-model mapping (pure; everything the UI shows comes here).

Honesty rules (binding, from the spec): citations/sources/timings/provenance
are mapped ONLY from the ``final`` SSE event's envelope — never synthesized
client-side; zero citations is an explicit honest state; ``guardrail_decisions``
is a Phase-3 stub (always ``[]``) and is NEVER rendered as active; the
Phoenix trace link exists only when the envelope carries a real ``trace``
block (omitted under the no-op tracer) AND the UI knows ``PHOENIX_ENDPOINT``;
an ``error`` event renders typed (detail + dependency + retry guidance) and
the partial streamed text is marked INCOMPLETE by the caller.

The reference for what "honest rendering" means is
``services/orchestrator/scripts/demo_live_query.py`` steps 6-10.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Phoenix project shared with the orchestrator (single-project decision, Q7).
PHOENIX_PROJECT = "orchestrator"

# Short form of config_sha256 shown in provenance (full value is in the trace).
_SHA_SHORT_LEN = 12


@dataclass(frozen=True)
class Citation:
    """One citation with its ``[start, end)`` claim_span resolved into text."""

    chunk_ids: list[str]
    start: int
    end: int
    claim_text: str


@dataclass(frozen=True)
class Source:
    """One retrieved chunk as shown in the sources panel."""

    rank: int
    chunk_id: str
    score: float
    text: str


@dataclass(frozen=True)
class NumberedSource:
    """
    A cited chunk assigned a stable per-response number for clickable citations.

    ``anchor`` (e.g. ``"[1]"``) is the SAME string that appears in the rewritten
    answer text AND that must be used as the Chainlit element ``name``. Chainlit's
    frontend matches element names against message content with a plain
    regex-escaped substring alternation (no word boundaries), so a bare ``"1"``
    would link EVERY ``1`` in the answer; the brackets delimit the token so
    ``"[1]"`` matches only the literal ``[1]``.
    """

    number: int
    anchor: str
    chunk_id: str
    rank: int
    score: float
    text: str


@dataclass(frozen=True)
class NumberedAnswer:
    """Answer text with cited ``[chunk_id]`` markers rewritten to ``[n]``, + sources."""

    text: str
    sources: list[NumberedSource]


@dataclass(frozen=True)
class TraceLink:
    """Phoenix trace pointer: endpoint URL + project + trace id."""

    url: str
    project: str
    trace_id: str


@dataclass(frozen=True)
class FinalRender:
    """Everything the UI renders after the ``final`` event."""

    answer_text: str
    citations: list[Citation]
    sources: list[Source]
    timings_ms: dict[str, float]
    pipeline_version: str
    config_sha256_short: str
    index: str
    trace: TraceLink | None
    # Phase-3 stubs: guardrail_decisions is required-but-empty and must NEVER
    # be rendered as active — this stays False regardless of the envelope.
    guardrails_active: bool = field(default=False, init=False)

    @property
    def has_citations(self) -> bool:
        """True when the model actually cited; False is reported honestly."""
        return bool(self.citations)


@dataclass(frozen=True)
class ErrorRender:
    """Typed render of one SSE ``error`` event (or a pre-stream HTTP error)."""

    detail: str
    dependency: str | None = None
    retry_after_seconds: int | None = None
    http_equivalent: int | None = None

    @property
    def retry_guidance(self) -> str:
        """Human retry guidance derived from ``retry_after_seconds``."""
        if self.retry_after_seconds is not None:
            return f"Transient — retry in about {self.retry_after_seconds} seconds."
        return "Check the dependency named above, then retry."


def build_trace_link(
    trace_block: dict[str, Any] | None,
    phoenix_endpoint: str | None,
    project_gid: str | None = None,
) -> TraceLink | None:
    """
    Build the Phoenix trace link — ONLY when the envelope carries ``trace``.

    ``trace`` is omitted under the orchestrator's no-op tracer; rendering a
    link then would claim tracing that never happened. Likewise, without
    ``PHOENIX_ENDPOINT`` the UI has no Phoenix to link to.

    Phoenix routes project pages by GID, not by name, so the link targets
    ``/projects/{project_gid}`` when the GID was resolved at startup; if it
    was not (Phoenix old/unreachable at boot), the link falls back to the
    Phoenix base URL rather than a name-based path that would not resolve. The
    ``trace_id`` is always shown in the label so the user can locate the exact
    trace (the envelope carries no span GID to deep-link to a single trace).
    """
    if not trace_block or not phoenix_endpoint:
        return None
    project = trace_block.get("phoenix_project", PHOENIX_PROJECT)
    base = phoenix_endpoint.rstrip("/")
    url = f"{base}/projects/{project_gid}" if project_gid else base
    return TraceLink(url=url, project=project, trace_id=trace_block["trace_id"])


def map_final_envelope(
    envelope: dict[str, Any], *, phoenix_endpoint: str | None, project_gid: str | None = None
) -> FinalRender:
    """
    Map the ``final`` event's QueryResponse envelope to the render model.

    Citations resolve their ``[start, end)`` ``claim_span`` offsets into the
    answer text; sources carry rank/chunk_id/score/text from
    ``retrieved_chunks``; timings come from the real ``timings_ms``;
    provenance from ``system_version`` (``config_sha256`` short form,
    ``pipeline_version``, resolved index).
    """
    result = envelope["result"]
    answer_text: str = result["answer"]["text"]

    citations = []
    for raw in result["answer"]["citations"]:
        start, end = raw["claim_span"]
        citations.append(
            Citation(
                chunk_ids=list(raw["chunk_ids"]),
                start=start,
                end=end,
                claim_text=answer_text[start:end].strip(),
            )
        )

    sources = [
        Source(
            rank=chunk["rank"],
            chunk_id=chunk["chunk_id"],
            score=float(chunk["score"]),
            text=chunk["text"],
        )
        for chunk in result["retrieved_chunks"]
    ]

    system_version = result["system_version"]
    return FinalRender(
        answer_text=answer_text,
        citations=citations,
        sources=sources,
        timings_ms=dict(result["timings_ms"]),
        pipeline_version=system_version["pipeline_version"],
        config_sha256_short=system_version["config_sha256"][:_SHA_SHORT_LEN],
        index=system_version["opensearch_index"],
        trace=build_trace_link(result.get("trace"), phoenix_endpoint, project_gid),
    )


def number_citations(final: FinalRender) -> NumberedAnswer:
    """
    Assign stable 1-based numbers to cited chunks and rewrite inline markers.

    Pure transform over the render model (no Chainlit imports). For each DISTINCT
    cited ``chunk_id`` that is present in ``retrieved_chunks`` (so it has evidence
    to show), assign a number by order of first ``[chunk_id]`` marker appearance
    in the answer text — reading order, deterministic — and rewrite each such
    ``[chunk_id]`` to ``[n]``. The returned ``anchor`` (``"[n]"``) is the exact
    string to use BOTH as the in-text marker and as the Chainlit element name.

    Honesty rules preserved:
    - Uncited markers (a ``[chunk_id]`` the model emitted but did NOT list in
      ``citations``, e.g. an unsupported aside) are LEFT UNTOUCHED — no number,
      no source, no pane. The bracket-delimited replace is order-independent and
      never mangles a literal ``[n]`` already in the prose.
    - A cited ``chunk_id`` absent from ``retrieved_chunks`` (contract violation)
      gets no number and its marker stays raw — we never fabricate a pane.
    - Zero citations → answer text returned unchanged, empty sources.
    """
    source_by_id = {source.chunk_id: source for source in final.sources}

    cited_with_evidence: list[str] = []
    for citation in final.citations:
        for chunk_id in citation.chunk_ids:
            if chunk_id in source_by_id and chunk_id not in cited_with_evidence:
                cited_with_evidence.append(chunk_id)

    def first_marker_pos(chunk_id: str) -> tuple[int, int]:
        # Order by first marker appearance; ids whose marker is absent from the
        # text sort last (by citation order) so numbering stays deterministic.
        found = final.answer_text.find(f"[{chunk_id}]")
        appears = 0 if found >= 0 else 1
        return (appears, found if found >= 0 else cited_with_evidence.index(chunk_id))

    ordered = sorted(cited_with_evidence, key=first_marker_pos)

    sources: list[NumberedSource] = []
    number_by_id: dict[str, int] = {}
    for number, chunk_id in enumerate(ordered, start=1):
        number_by_id[chunk_id] = number
        source = source_by_id[chunk_id]
        sources.append(
            NumberedSource(
                number=number,
                anchor=f"[{number}]",
                chunk_id=chunk_id,
                rank=source.rank,
                score=source.score,
                text=source.text,
            )
        )

    text = final.answer_text
    for chunk_id, number in number_by_id.items():
        # Bracket-delimited literal replace: `[c:1]` cannot match inside `[c:12]`
        # (the closing `]` differs), so replacement order is irrelevant and only
        # cited markers are touched.
        text = text.replace(f"[{chunk_id}]", f"[{number}]")

    return NumberedAnswer(text=text, sources=sources)


def map_error_event(payload: dict[str, Any]) -> ErrorRender:
    """Map one typed SSE ``error`` payload (detail + taxonomy fields)."""
    return ErrorRender(
        detail=payload["detail"],
        dependency=payload.get("dependency"),
        retry_after_seconds=payload.get("retry_after_seconds"),
        http_equivalent=payload.get("http_equivalent"),
    )


def format_final_details(render: FinalRender, numbered: NumberedAnswer | None = None) -> str:
    """
    Markdown block shown AFTER the streamed answer (final-envelope data only).

    Zero citations is stated honestly; guardrails are never mentioned as
    active (``guardrails_active`` is False by construction).

    When ``numbered`` is supplied (the same ``NumberedAnswer`` used to rewrite
    the answer's markers), a "Cited sources" list cross-references the SAME
    ``[n]`` anchors that are clickable in the answer message, so the two blocks
    agree. Omitting it (or empty sources) leaves the block byte-for-byte as
    before — the zero-citation and legacy call paths are unchanged.
    """
    lines: list[str] = []

    if numbered is not None and numbered.sources:
        # ONE numbering scheme, matching the answer's clickable [n] markers.
        # "Cited sources" lists the distinct retrieved chunks (the [n] you click);
        # "Claims" maps each cited sentence to the SAME [n]. We deliberately do NOT
        # emit the old 0-based per-citation "[i]" list here — two bracketed schemes
        # in one response read as disagreeing numbers (they don't; they're
        # different axes) and confuse which [n] a click opens.
        number_by_chunk = {source.chunk_id: source.number for source in numbered.sources}

        lines.append(
            f"**Cited sources** ({len(numbered.sources)}) — click a [n] marker in "
            "the answer to open that retrieved chunk"
        )
        for source in numbered.sources:
            # Labelled a retrieved chunk (never "the source document"), honesty rule.
            lines.append(
                f"- {source.anchor} retrieved chunk — rank {source.rank}, "
                f"score {source.score:.4f}, chunk_id {source.chunk_id}"
            )

        if render.citations:
            lines.append("")
            lines.append(f"**Claims** ({len(render.citations)})")
            for citation in render.citations:
                claim = citation.claim_text.replace("\n", " ")
                # Reference the chunk's [n] (the clickable anchor), not a 0-based
                # citation index. A cited chunk absent from retrieved_chunks
                # (contract violation) keeps its raw id — never fabricated.
                refs = [
                    f"[{number_by_chunk[cid]}]"
                    for cid in citation.chunk_ids
                    if cid in number_by_chunk
                ]
                refs += [cid for cid in citation.chunk_ids if cid not in number_by_chunk]
                ref_str = ", ".join(refs) if refs else "—"
                lines.append(
                    f'- cites {ref_str} — span [{citation.start},{citation.end}): "{claim}"'
                )
    elif render.has_citations:
        # Legacy / numbered omitted: the original 0-based per-citation list.
        lines.append(f"**Citations** ({len(render.citations)})")
        for i, citation in enumerate(render.citations):
            claim = citation.claim_text.replace("\n", " ")
            ids = ", ".join(citation.chunk_ids)
            lines.append(
                f'- [{i}] chunks {ids} — span [{citation.start},{citation.end}): "{claim}"'
            )
    else:
        lines.append("**Citations**: 0 — the model answered without citing (reported honestly).")

    lines.append("")
    lines.append("**Timings (ms)**")
    for stage, ms in render.timings_ms.items():
        lines.append(f"- {stage}: {ms:.1f}")

    lines.append("")
    lines.append("**Provenance**")
    lines.append(f"- pipeline_version: {render.pipeline_version}")
    lines.append(f"- config_sha256: {render.config_sha256_short}…")
    lines.append(f"- index: {render.index}")

    if render.trace is not None:
        lines.append("")
        lines.append(
            f"**Phoenix trace**: [{render.trace.trace_id}]({render.trace.url}) "
            f"(project '{render.trace.project}')"
        )

    return "\n".join(lines)


def format_error(render: ErrorRender, *, had_partial_text: bool) -> str:
    """Markdown for an error turn: typed detail, dependency, retry guidance."""
    lines = ["**The stream failed before completing.**", "", f"Detail: {render.detail}"]
    if render.dependency is not None:
        lines.append(f"Dependency: {render.dependency}")
    if render.http_equivalent is not None:
        lines.append(f"HTTP equivalent: {render.http_equivalent}")
    lines.append(render.retry_guidance)
    if had_partial_text:
        lines.append("")
        lines.append(
            "The partial answer above is INCOMPLETE — do not rely on it. "
            "This turn was NOT added to the session history."
        )
    return "\n".join(lines)
