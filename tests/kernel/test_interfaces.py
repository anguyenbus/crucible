"""Tests for the moved kernel RagAdapter and the kernel interface protocols.

Phase 2 (Commit 7) moves the concrete RagAdapter into
``crucible.kernel.interfaces`` and switches its schema load to the kernel
importlib.resources default (Finding A fix on the kernel path). The required
query_callable TypeError and embedder passthrough are preserved.

Deterministic only (NO hypothesis).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crucible.kernel.interfaces import (
    ClaimStore,
    JudgeProvider,
    RagAdapter,
    RateLimiter,
)

# A fully-conformant rag_query_output payload (so the importlib.resources schema
# load + validate path is exercised on a passing case).
_VALID_RAG_OUTPUT = {
    "schema_version": "1.0.0",
    "system_version": {"pipeline_version": "1.0.0"},
    "query": {"query_id": "q1", "text": "What is the termination clause?"},
    "answer": {"text": "The contract may be terminated with notice.", "citations": []},
    "retrieved_chunks": [],
}


def test_bare_construction_raises_type_error() -> None:
    """A bare RagAdapter() (no query_callable) must raise TypeError pointing at the fix."""
    with pytest.raises(TypeError) as exc_info:
        _ = RagAdapter()

    message = str(exc_info.value)
    assert "query_callable" in message
    assert re.search(r"demo|stub", message, re.IGNORECASE)


def test_embedder_passthrough_when_callable_accepts_it() -> None:
    """The shared embedder is passed through when the query callable accepts it."""
    seen = {}

    def query_with_embedder(question, corpus_dir, embedder=None):  # noqa: ANN001, ANN202
        seen["embedder"] = embedder
        return _VALID_RAG_OUTPUT

    sentinel_embedder = object()
    adapter = RagAdapter(query_callable=query_with_embedder, embedder=sentinel_embedder)

    out = adapter.query("Q?", Path("corpus"))

    assert out == _VALID_RAG_OUTPUT
    assert seen["embedder"] is sentinel_embedder


def test_schema_load_uses_packaged_resources_from_anywhere(tmp_path, monkeypatch) -> None:
    """Schema validation resolves via importlib.resources, not a CWD path."""

    def query(question, corpus_dir):  # noqa: ANN001, ANN202
        return _VALID_RAG_OUTPUT

    adapter = RagAdapter(query_callable=query)

    # Run from a directory with NO src/crucible/contracts/ on disk: a CWD-relative
    # path would FileNotFoundError; importlib.resources resolves regardless.
    monkeypatch.chdir(tmp_path)
    out = adapter.query("Q?", Path("corpus"))
    assert out == _VALID_RAG_OUTPUT


def test_protocols_are_defined() -> None:
    """JudgeProvider / RateLimiter / ClaimStore protocols are exported from the kernel."""
    assert JudgeProvider is not None
    assert RateLimiter is not None
    assert ClaimStore is not None
