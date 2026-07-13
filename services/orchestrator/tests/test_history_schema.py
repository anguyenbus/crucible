"""``history`` request-schema extension tests (chainlit-chat-ui Task Group 5).

The field is REQUEST-ONLY: bounded (max 20 turns, 8,000 chars per turn),
``extra="forbid"`` on each turn, accepted on BOTH query routes, and NEVER
echoed anywhere in ``result`` — so eval's rag_query_output v1.1.0 contract is
provably unchanged and absent ``history`` ≡ pre-Phase-B behavior exactly
(byte-parity against the committed Phase A golden).

Focused checks only (per task 5.1) — exhaustive per-field validation
permutations are intentionally skipped.
"""

from __future__ import annotations

import itertools
import json
import time

import pytest
from pydantic import ValidationError

from tests.test_query_regression import FIXED_REQUEST, GOLDEN_RESPONSE

VALID_HISTORY = [
    {"role": "user", "text": "What does Division 38 of the GST Act cover?"},
    {"role": "assistant", "text": "Division 38 lists the supplies that are GST-free."},
]

REQUEST_WITH_HISTORY = {
    "question": "Does it apply to services supplied to a non-resident?",
    "query_id": "q-history-001",
    "pipeline_config": "legal-rag-default-1.1.0",
    "metadata": {"category": "gst"},
    "history": VALID_HISTORY,
}


@pytest.fixture
def deterministic_clock(monkeypatch):
    """perf_counter → a fixed-step counter so timings_ms bytes are stable."""
    ticks = itertools.count()
    monkeypatch.setattr(time, "perf_counter", lambda: next(ticks) * 0.001)


def test_history_turn_rejects_unknown_keys(client):
    """HistoryTurn is extra="forbid": unknown keys fail loudly (422 via the route)."""
    from app.schemas.query import HistoryTurn

    with pytest.raises(ValidationError):
        HistoryTurn.model_validate({"role": "user", "text": "hi", "timestamp": "2026-07-13"})

    smuggled = {
        **REQUEST_WITH_HISTORY,
        "history": [{"role": "user", "text": "hi", "session_id": "s-1"}],
    }
    assert client.post("/query", json=smuggled).status_code == 422


def test_history_bounds_rejected_with_422(client):
    """More than 20 turns, or a single turn over 8,000 chars, is rejected."""
    too_many_turns = {
        **REQUEST_WITH_HISTORY,
        "history": [{"role": "user", "text": f"turn {i}"} for i in range(21)],
    }
    assert client.post("/query", json=too_many_turns).status_code == 422

    oversized_turn = {
        **REQUEST_WITH_HISTORY,
        "history": [{"role": "user", "text": "x" * 8_001}],
    }
    assert client.post("/query", json=oversized_turn).status_code == 422

    invalid_role = {
        **REQUEST_WITH_HISTORY,
        "history": [{"role": "system", "text": "not a chat turn"}],
    }
    assert client.post("/query", json=invalid_role).status_code == 422


def test_history_is_never_echoed_anywhere_in_result(client):
    """result.query.text echoes the CURRENT question; no history text leaks."""
    sentinel = "HISTORY-SENTINEL-b2c1a0"
    request = {
        **REQUEST_WITH_HISTORY,
        "history": [
            {"role": "user", "text": f"prior user turn {sentinel}"},
            {"role": "assistant", "text": f"prior assistant turn {sentinel}"},
        ],
    }

    response = client.post("/query", json=request)
    assert response.status_code == 200
    body = response.json()

    result = body["result"]
    assert result["query"]["text"] == request["question"]
    # metadata stays the opaque verbatim echo — history never rides in it.
    assert result["query"]["metadata"] == {"category": "gst"}
    assert sentinel not in json.dumps(body), (
        "history is INPUT-ONLY and must never be echoed anywhere in the response"
    )


def test_history_is_accepted_on_both_query_routes(client):
    """The optional field lands on POST /query AND POST /query/stream."""
    blocking = client.post("/query", json=REQUEST_WITH_HISTORY)
    assert blocking.status_code == 200

    streamed = client.post("/query/stream", json=REQUEST_WITH_HISTORY)
    assert streamed.status_code == 200
    assert "event: final" in streamed.text
    final_data = streamed.text.split("event: final\ndata: ", 1)[1].split("\n\n", 1)[0]
    assert json.loads(final_data)["result"]["query"]["text"] == REQUEST_WITH_HISTORY["question"]


def test_absent_history_is_byte_identical_to_the_phase_a_golden(client, deterministic_clock):
    """Single-turn regression: no history → the committed golden bytes, unchanged."""
    response = client.post("/query", json=FIXED_REQUEST)
    assert response.status_code == 200
    assert response.content == GOLDEN_RESPONSE.read_bytes(), (
        "POST /query with history absent must stay byte-identical to the "
        "pre-Phase-B golden capture (absent history ≡ today)."
    )
