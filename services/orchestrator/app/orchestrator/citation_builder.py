"""
Citation construction stage: bracketed chunk-id markers → ``citations[]``.

Deterministic PURE function — no NLP, no sentence splitting (legal text
breaks regex splitters). Markers are bracketed verbatim chunk ids in the
``{doc_id}:{chunk_idx}`` format the prompt template instructs; they STAY in
``answer.text`` (eval ClaudeGenerator convention: judge-visibility parity,
stable offsets).

Validation: only marker ids present in the retrieved set become citations.
Unknown-id markers are DROPPED, never errors — but COUNTED, and the count is
returned as data so the router can surface it as a span attribute
(hallucinated-citation signal for eval).

``claim_span`` is the ``[start, end)`` character interval from the end of the
PREVIOUS marker (or answer start) to the end of the CURRENT marker. Every
marker — known or unknown — advances the boundary, so text attributed to a
dropped (hallucinated) marker is not silently re-attributed to the next
citation.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

# Bracketed verbatim marker, e.g. "[gst-act-1999:4]". Any bracketed token is
# extracted; validation against the retrieved set decides what survives.
_MARKER_RE = re.compile(r"\[([^\[\]]+)\]")


@dataclass(frozen=True)
class CitationBuildResult:
    """Citations plus the dropped-unknown-marker count (plain data)."""

    citations: list[dict[str, Any]]
    dropped_unknown_marker_count: int


def build_citations(
    answer_text: str,
    retrieved_chunk_ids: Collection[str],
) -> CitationBuildResult:
    """
    Extract and validate citation markers from the generated answer.

    Args:
        answer_text: Generated answer, markers included (never modified).
        retrieved_chunk_ids: ``chunk_id`` values of the retrieved set.

    Returns:
        Schema-shaped ``citations[]`` entries (``claim_span`` + ``chunk_ids``)
        for known-id markers, in answer order, plus the count of dropped
        unknown-id markers.

    """
    known_ids = set(retrieved_chunk_ids)
    citations: list[dict[str, Any]] = []
    dropped = 0
    segment_start = 0
    for match in _MARKER_RE.finditer(answer_text):
        if match.group(1) in known_ids:
            citations.append(
                {
                    "claim_span": [segment_start, match.end()],
                    "chunk_ids": [match.group(1)],
                }
            )
        else:
            dropped += 1
        segment_start = match.end()
    return CitationBuildResult(citations=citations, dropped_unknown_marker_count=dropped)
