"""
Per-session conversation history: wire shape + client-side windowing.

History turns use the WIRE shape of the orchestrator's ``history`` request
field (``{"role": "user"|"assistant", "text": str}``) so the session list is
sent verbatim. The bounds below MIRROR the orchestrator's schema constants
(``app/schemas/query.py``: ``MAX_HISTORY_TURNS`` / ``MAX_HISTORY_TURN_CHARS``)
— duplicated by design, never imported (the dependency firewall forbids
``app.*``); the schema rejects violations with 422, so the client truncates
to the SAME bounds before sending.

Turns are appended ONLY after a COMPLETED turn (a stream that ended in
``final``); a turn that ended in ``error`` never enters history — resending
text the model never finished would be dishonest context.
"""

from __future__ import annotations

from typing import Any

# Mirror of the orchestrator's schema-enforced request bounds (never widen).
MAX_HISTORY_TURNS = 20
MAX_HISTORY_TURN_CHARS = 8_000

HistoryTurn = dict[str, str]


def bound_history(history: list[HistoryTurn]) -> list[HistoryTurn]:
    """
    Truncate to the schema bounds: per-turn chars, then oldest turns first.

    Each turn's text is clipped to ``MAX_HISTORY_TURN_CHARS``; when more than
    ``MAX_HISTORY_TURNS`` turns remain, the OLDEST are dropped (the list is
    oldest-first, so the newest tail is kept).
    """
    clipped = [
        {"role": turn["role"], "text": turn["text"][:MAX_HISTORY_TURN_CHARS]} for turn in history
    ]
    return clipped[-MAX_HISTORY_TURNS:]


def append_completed_turn(
    history: list[HistoryTurn], question: str, answer_text: str
) -> list[HistoryTurn]:
    """
    Return the new session history after one COMPLETED turn.

    Appends the user question and the final answer text (from the ``final``
    envelope), then re-applies the bounds. Callers must NOT call this for a
    turn that ended in an ``error`` event.
    """
    return bound_history(
        [
            *history,
            {"role": "user", "text": question},
            {"role": "assistant", "text": answer_text},
        ]
    )


def history_after_turn(
    history: list[HistoryTurn], question: str, outcome: Any
) -> list[HistoryTurn]:
    """
    Return the new history for a turn outcome: grown on ``final``, unchanged on error.

    ``outcome`` is a render model from :mod:`chat_ui.render` —
    ``FinalRender`` (completed) or ``ErrorRender`` (failed). Keeping the
    decision here makes the "failed turns never enter history" rule a plain
    testable function instead of Chainlit-callback control flow.
    """
    from chat_ui.render import FinalRender

    if isinstance(outcome, FinalRender):
        return append_completed_turn(history, question, outcome.answer_text)
    return list(history)
