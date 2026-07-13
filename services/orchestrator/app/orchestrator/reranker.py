"""
Reranking stage.

Parked stage: typed IDENTITY on the ranked chunk list — reranking is parked
per the roadmap, but the seam stays in-chain at its natural position.
"""

from __future__ import annotations

from typing import Any


def rerank(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Typed identity on the ranked chunk list (reranking parked)."""
    return chunks
