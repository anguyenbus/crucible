"""
Input/output guardrail handling stage.

Phase 3 wires Bedrock Guardrails and populates the envelope's
``guardrail_decisions[]``; until then every response carries a
required-but-empty array.

Parked stage: typed IDENTITIES on the input question text and the output
answer text — the two natural positions guardrails run at.
"""

from __future__ import annotations


def check_input(question: str) -> str:
    """Typed identity on the input question text (guardrails parked)."""
    return question


def check_output(answer_text: str) -> str:
    """Typed identity on the output answer text (guardrails parked)."""
    return answer_text
