"""Envelope→render-model mapping tests (final envelope + error events)."""

from chat_ui.history import history_after_turn
from chat_ui.render import (
    format_error,
    format_final_details,
    map_error_event,
    map_final_envelope,
)

_ANSWER = "Alpha claim [c:1]. Beta claim [c:2]."


def _envelope(*, citations=True, trace=True):
    """A final-event QueryResponse envelope shaped like POST /query's 200 body."""
    result = {
        "schema_version": "1.1.0",
        "system_version": {
            "pipeline_version": "1.2.0",
            "config_sha256": "abc123def456" + "0" * 52,
            "generator_model": "au.anthropic.claude-sonnet-4-6",
            "embedder_model": "amazon.titan-embed-text-v2:0",
            "opensearch_index": "legal-rag-bench",
        },
        "query": {"query_id": "q-1", "text": "What must be proven?"},
        "answer": {
            "text": _ANSWER,
            "citations": (
                [
                    {"chunk_ids": ["c:1"], "claim_span": [0, 18]},
                    {"chunk_ids": ["c:2"], "claim_span": [18, 36]},
                ]
                if citations
                else []
            ),
        },
        "retrieved_chunks": [
            {"rank": 1, "chunk_id": "c:1", "score": 0.91, "doc_id": "c", "text": "chunk one"},
            {"rank": 2, "chunk_id": "c:2", "score": 0.72, "doc_id": "c", "text": "chunk two"},
        ],
        "timings_ms": {"embedding": 12.5, "retrieval": 40.0, "generation": 900.0, "total": 980.0},
    }
    if trace:
        result["trace"] = {
            "trace_id": "ab" * 16,
            "span_id": "cd" * 8,
            "phoenix_project": "orchestrator",
        }
    return {"result": result, "guardrail_decisions": [], "generation_mode": "live"}


def test_final_envelope_maps_citations_sources_timings_and_provenance():
    render = map_final_envelope(_envelope(), phoenix_endpoint="http://localhost:6006")

    # Citations resolved by mapping [start, end) claim_spans into the answer.
    assert [c.claim_text for c in render.citations] == [
        "Alpha claim [c:1].",
        "Beta claim [c:2].",
    ]
    assert render.citations[0].chunk_ids == ["c:1"]
    assert (render.citations[1].start, render.citations[1].end) == (18, 36)

    # Sources carry rank, chunk_id, score, text from retrieved_chunks.
    assert [(s.rank, s.chunk_id, s.score, s.text) for s in render.sources] == [
        (1, "c:1", 0.91, "chunk one"),
        (2, "c:2", 0.72, "chunk two"),
    ]

    # Timings come from the real timings_ms; provenance from system_version.
    assert render.timings_ms["generation"] == 900.0
    assert render.pipeline_version == "1.2.0"
    assert render.config_sha256_short == "abc123def456"  # short form
    assert render.index == "legal-rag-bench"


def test_zero_citations_is_honest_and_guardrails_never_render_active():
    render = map_final_envelope(_envelope(citations=False), phoenix_endpoint=None)

    assert render.has_citations is False
    # guardrail_decisions: [] must NEVER map to an "active guardrails" state.
    assert render.guardrails_active is False

    details = format_final_details(render)
    assert "0" in details and "without citing" in details
    assert "guardrail" not in details.lower()


def test_trace_link_uses_project_gid_and_falls_back_to_base_url():
    # With the resolved GID, the link targets the GID route (Phoenix routes
    # projects by GID, not name); the trace_id rides in the label.
    with_gid = map_final_envelope(
        _envelope(trace=True), phoenix_endpoint="http://localhost:6006", project_gid="UHJvamVjdDoy"
    )
    assert with_gid.trace is not None
    assert with_gid.trace.url == "http://localhost:6006/projects/UHJvamVjdDoy"
    assert with_gid.trace.project == "orchestrator"
    assert with_gid.trace.trace_id == "ab" * 16

    # GID unresolved at boot (Phoenix old/unreachable) → base-URL fallback,
    # never a name-based path that would not resolve in Phoenix.
    no_gid = map_final_envelope(_envelope(trace=True), phoenix_endpoint="http://localhost:6006")
    assert no_gid.trace is not None
    assert no_gid.trace.url == "http://localhost:6006"

    # trace omitted (no-op tracer on the service side) → no link, ever.
    assert map_final_envelope(_envelope(trace=False), phoenix_endpoint="http://x").trace is None
    # PHOENIX_ENDPOINT unset on the UI side → nothing to link to.
    assert map_final_envelope(_envelope(trace=True), phoenix_endpoint=None).trace is None
    assert "Phoenix trace" not in format_final_details(
        map_final_envelope(_envelope(trace=False), phoenix_endpoint="http://x")
    )


def test_error_event_renders_typed_and_failed_turn_never_enters_history():
    error = map_error_event(
        {
            "detail": "Bedrock throttling persisted past the retry budget.",
            "dependency": "bedrock",
            "retry_after_seconds": 30,
            "http_equivalent": 503,
        }
    )

    assert error.dependency == "bedrock"
    assert "30" in error.retry_guidance
    text = format_error(error, had_partial_text=True)
    assert "Bedrock throttling" in text
    assert "bedrock" in text
    assert "INCOMPLETE" in text

    # The failed turn is NOT appended; a completed turn is.
    history = [{"role": "user", "text": "earlier"}, {"role": "assistant", "text": "answer"}]
    assert history_after_turn(history, "failed question", error) == history
    final = map_final_envelope(_envelope(), phoenix_endpoint=None)
    grown = history_after_turn(history, "good question", final)
    assert grown[-2:] == [
        {"role": "user", "text": "good question"},
        {"role": "assistant", "text": _ANSWER},
    ]


def test_no_trace_and_zero_citations_simultaneously_all_render_honestly():
    """Gap: trace ABSENT and citations EMPTY in ONE envelope (endpoint set)."""
    # The no-op-tracer + no-citations turn: even WITH PHOENIX_ENDPOINT known,
    # an absent trace block must yield no link, zero citations stay honest, and
    # the guardrail stub is never rendered active — all at once.
    render = map_final_envelope(
        _envelope(citations=False, trace=False), phoenix_endpoint="http://localhost:6006"
    )

    assert render.trace is None  # absent trace → no link, despite a known endpoint
    assert render.has_citations is False
    assert render.guardrails_active is False

    details = format_final_details(render)
    assert "0" in details and "without citing" in details
    assert "Phoenix trace" not in details
    assert "guardrail" not in details.lower()
