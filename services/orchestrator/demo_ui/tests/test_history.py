"""History windowing tests: client-side bounds mirror the request schema."""

from chat_ui.history import (
    MAX_HISTORY_TURN_CHARS,
    MAX_HISTORY_TURNS,
    append_completed_turn,
    bound_history,
)


def test_windowing_matches_schema_bounds_oldest_dropped_first():
    # 25 turns: more than the 20-turn bound; one turn exceeds the char bound.
    history = [{"role": "user", "text": f"turn {i}"} for i in range(25)]
    history[24] = {"role": "user", "text": "x" * (MAX_HISTORY_TURN_CHARS + 2_000)}

    bounded = bound_history(history)

    assert len(bounded) == MAX_HISTORY_TURNS == 20
    # Oldest dropped first: the first surviving turn is original index 5.
    assert bounded[0]["text"] == "turn 5"
    # Per-turn text clipped to exactly the schema's 8,000-char bound.
    assert len(bounded[-1]["text"]) == MAX_HISTORY_TURN_CHARS == 8_000

    # Completed turns append user question + assistant answer, oldest-first.
    appended = append_completed_turn([], "What is theft?", "Theft is... [c:1]")
    assert appended == [
        {"role": "user", "text": "What is theft?"},
        {"role": "assistant", "text": "Theft is... [c:1]"},
    ]
