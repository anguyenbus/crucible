"""
Citation extraction for RAG answers.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the extract_citations
function which extracts sentence-to-chunk citations from generated answers.
"""

from __future__ import annotations

import re
from typing import Any


def extract_citations(
    answer: str, retrieved_chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Extract simple sentence-to-chunk citations from answer text.

    This function looks for chunk_id references in the answer text (e.g.,
    [doc1_chunk_00000]) and maps them to claim spans.

    Args:
        answer: Generated answer text from Claude.
        retrieved_chunks: List of retrieved chunks with chunk_id field.

    Returns:
        List of citation dictionaries, each containing:
            - claim_span: [start, end) character offsets in answer
            - chunk_ids: List of chunk_ids referenced in this claim

    Example:
        >>> answer = "The answer is [doc1_chunk_00000]."
        >>> chunks = [{"chunk_id": "doc1_chunk_00000", "text": "..."}]
        >>> citations = extract_citations(answer, chunks)
        >>> len(citations)
        1

    """
    citations = []

    # Get valid chunk IDs
    valid_chunk_ids = {chunk.get("chunk_id", "") for chunk in retrieved_chunks}

    # Find all chunk_id references in the answer
    # Pattern matches [doc1_chunk_00000] style references
    pattern = r"\[([a-zA-Z0-9_]+_chunk_\d+)\]"
    matches = list(re.finditer(pattern, answer))

    for match in matches:
        chunk_id = match.group(1)

        # Only include references to valid chunks
        if chunk_id not in valid_chunk_ids:
            continue

        # Find the sentence containing this citation
        start = match.start()
        end = match.end()

        # Expand to include the full sentence
        # Find sentence boundaries
        sentence_start = answer.rfind(".", 0, start) + 1
        if sentence_start == 0:
            # No period found, start from beginning or after newline
            sentence_start = answer.rfind("\n", 0, start) + 1

        sentence_end = answer.find(".", end)
        if sentence_end == -1:
            sentence_end = len(answer)
        else:
            sentence_end += 1  # Include the period

        # Trim whitespace
        claim_text = answer[sentence_start:sentence_end].strip()
        claim_start = answer.find(claim_text)
        claim_end = claim_start + len(claim_text)

        citations.append(
            {
                "claim_span": [claim_start, claim_end],
                "chunk_ids": [chunk_id],
            }
        )

    # Remove duplicate citations (same span)
    seen = set()
    unique_citations = []
    for citation in citations:
        key = tuple(citation["claim_span"])
        if key not in seen:
            seen.add(key)
            unique_citations.append(citation)

    return unique_citations
