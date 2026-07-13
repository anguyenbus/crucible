"""
Context assembly stage: rank-ordered ``[chunk_id]: text`` blocks.

Deterministic PURE function of the ranked chunks plus the config-pinned
CHARACTER budget (no tokenizer dependency). Truncation is whole-chunk and
tail-only: a chunk either fits entirely within the budget or it — and every
chunk ranked after it — is dropped.
"""

from __future__ import annotations

from typing import Any

# Separator between consecutive context blocks (counted against the budget).
_BLOCK_SEPARATOR = "\n\n"


def assemble_context(chunks: list[dict[str, Any]], *, char_budget: int) -> str:
    """
    Format retrieved chunks into the context string under the budget.

    Args:
        chunks: Ranked ``retrieved_chunks`` dicts (``chunk_id``, ``text``).
        char_budget: Config-pinned maximum length of the returned string.

    Returns:
        Rank-ordered ``[chunk_id]: text`` blocks joined by blank lines; at
        most ``char_budget`` characters via whole-chunk tail truncation.

    """
    assembled = ""
    for chunk in chunks:
        block = f"[{chunk['chunk_id']}]: {chunk['text']}"
        candidate = block if not assembled else f"{assembled}{_BLOCK_SEPARATOR}{block}"
        if len(candidate) > char_budget:
            break
        assembled = candidate
    return assembled
