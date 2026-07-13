"""
Query rewrite stage: deterministic history-aware retrieval-query building.

PURE function of typed inputs + config pins — DETERMINISTIC by decision, so
each turn still costs exactly ONE paid Bedrock generation (+ one Titan query
embedding). Honest limitation: deterministic prefixing does not resolve
pronouns/ellipsis as well as an LLM condense step; an LLM rewrite (pinned
haiku-class model) is a documented LATER upgrade requiring a new config
version. No client/infra imports, no env reads (``stages-pure`` + grep-gate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.schemas.pipeline_config import HistoryPins
    from app.schemas.query import HistoryTurn


def rewrite(
    question: str,
    history: Sequence[HistoryTurn] | None = None,
    pins: HistoryPins | None = None,
) -> str:
    """
    Build the retrieval query from the current question plus recent user turns.

    A config-pinned window of recent USER turns (``rewrite_window_user_turns``)
    is selected newest-first under ``rewrite_char_budget`` (whole-turn
    truncation: a turn that would overflow is dropped whole together with
    everything older — the kept turns are always a contiguous newest suffix;
    the budget counts turn text characters only). The kept turns are rendered
    oldest-first, newline-joined, and prefixed to the current question.

    Empty/absent history (or absent pins) → IDENTITY on the question, so a
    single-turn request behaves exactly as before Phase B.

    Args:
        question: The current (guardrail-checked, routed) user question.
        history: Prior turns, oldest first; only ``role == "user"`` turns are
            candidates for the rewrite prefix.
        pins: The resolved config's ``history`` pins block.

    Returns:
        The retrieval query — what embedding and retrieval actually run on
        (observable in the retrieval span's ``INPUT_VALUE``).

    """
    if not history or pins is None:
        return question

    selected: list[str] = []
    used_chars = 0
    for turn in reversed(history):
        if turn.role != "user":
            continue
        if len(selected) >= pins.rewrite_window_user_turns:
            break
        if used_chars + len(turn.text) > pins.rewrite_char_budget:
            break
        selected.append(turn.text)
        used_chars += len(turn.text)

    if not selected:
        return question
    selected.reverse()
    return "\n".join([*selected, question])
