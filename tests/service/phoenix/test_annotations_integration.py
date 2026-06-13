"""
Live Phoenix write-back integration test (Task Group 7.5).

Marked ``@pytest.mark.phoenix_integration`` so it is excluded from the default
hermetic run (``-m "not phoenix_integration"``) and auto-skipped by the
``tests/conftest.py`` collection hook when ``PHOENIX_ENDPOINT`` is unreachable.
Run explicitly against a live server with:

    uv run pytest tests/service/phoenix -m phoenix_integration
"""

from __future__ import annotations

import os

import pytest

from crucible.service.phoenix.annotations import write_back_annotations

pytestmark = pytest.mark.phoenix_integration


def test_live_write_back_does_not_raise() -> None:
    """A live best-effort write-back completes without raising."""
    from phoenix.client import Client

    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    client = Client(base_url=endpoint)

    # Best-effort: even against a live server, a bogus span id must not raise.
    write_back_annotations(
        client=client,
        span_id="nonexistent-span-for-integration-probe",
        scores={"faithfulness": 0.9},
        reasoning={"faithfulness": "integration probe"},
        chunk_verdicts=[{"score": 0.8, "explanation": "relevant"}],
        max_retries=0,
    )
