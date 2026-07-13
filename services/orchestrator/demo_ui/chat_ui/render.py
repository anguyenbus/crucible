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


def map_error_event(payload: dict[str, Any]) -> ErrorRender:
    """Map one typed SSE ``error`` payload (detail + taxonomy fields)."""
    return ErrorRender(
        detail=payload["detail"],
        dependency=payload.get("dependency"),
        retry_after_seconds=payload.get("retry_after_seconds"),
        http_equivalent=payload.get("http_equivalent"),
    )


def format_final_details(render: FinalRender) -> str:
    """
    Markdown block shown AFTER the streamed answer (final-envelope data only).

    Zero citations is stated honestly; guardrails are never mentioned as
    active (``guardrails_active`` is False by construction).
    """
    lines: list[str] = []

    if render.has_citations:
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
