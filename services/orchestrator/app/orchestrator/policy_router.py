"""
Pipeline-routing stage: decides the direct / RAG / tool path for a query.

NAMING HAZARD: this module is the PIPELINE-ROUTING stage (direct/RAG/tool),
NOT the external policy/authorization service. Retrieval-time authorization
and permission filtering are parked per the roadmap and live outside this
stage entirely.

Parked stage: typed IDENTITY on the question — routing logic is parked per
the roadmap, but the seam stays in-chain at its natural position.
"""

from __future__ import annotations


def route(question: str) -> str:
    """Typed identity on the question (routing logic parked)."""
    return question
