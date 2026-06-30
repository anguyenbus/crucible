"""Tests for the RagAdapter constructor contract.

The RagAdapter no longer silently defaults to the demo stub query. A bare
``RagAdapter()`` must fail loudly so non-demo contexts never pick up a demo
backend by accident.
"""

import re

import pytest

from app.kernel.interfaces import RagAdapter


def test_bare_construction_raises_type_error():
    """A bare RagAdapter() (no query_callable) must raise TypeError."""
    with pytest.raises(TypeError):
        _ = RagAdapter()


def test_bare_construction_message_points_at_fix():
    """The TypeError message names the fix: pass query_callable / demo extra."""
    with pytest.raises(TypeError) as exc_info:
        _ = RagAdapter()

    message = str(exc_info.value)
    assert "query_callable" in message
    # Message should steer the demo user toward the stub query / demo extra.
    assert re.search(r"demo|stub", message, re.IGNORECASE)


def test_explicit_query_callable_is_used():
    """Passing query_callable explicitly constructs and is wired through."""

    def fake_query(question, corpus_dir):  # noqa: ANN001, ANN202
        return {"answer": "ok"}

    adapter = RagAdapter(query_callable=fake_query)
    assert adapter._query is fake_query
