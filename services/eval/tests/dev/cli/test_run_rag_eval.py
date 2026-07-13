"""Tests for RAG eval CLI integration and dataset resolver."""

import tempfile
from pathlib import Path

from dev.cli.run_rag_eval import load_dataset


def test_load_dataset_routes_gst_prefix():
    """Test that gst_* prefix routes to GST loader."""
    # Create mock config with gst_legal_rag
    config = {"datasets": {"gst_legal_rag": {"cache_path": "test_cache"}}}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_dir = tmp_path / "test_cache"
        cache_dir.mkdir(parents=True)

        # Create mock questions.jsonl
        questions_file = cache_dir / "questions.jsonl"
        questions_file.write_text(
            '{"id": "1", "question": "Q", "answer": "A", "relevant_passage_id": "P"}\n'
        )

        # Load with gst_pico slice - override cache path in config
        config["datasets"]["gst_legal_rag"]["cache_path"] = str(cache_dir)
        results = list(load_dataset("gst_pico", config))

        assert len(results) == 1
        assert results[0] == ("1", "Q", "P", "A")


def test_load_dataset_routes_gst_to_gst_loader():
    """Test that gst_* prefix uses GST loader, not legal_rag_bench."""

    config = {"datasets": {"gst_legal_rag": {"cache_path": "test_cache"}}}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_dir = tmp_path / "test_cache"
        cache_dir.mkdir(parents=True)

        # Create mock questions.jsonl
        questions_file = cache_dir / "questions.jsonl"
        questions_file.write_text(
            '{"id": "1", "question": "Q", "answer": "A", "relevant_passage_id": "P"}\n'
        )

        config["datasets"]["gst_legal_rag"]["cache_path"] = str(cache_dir)
        results = list(load_dataset("gst_pico", config))

        # Verify we got 1 result from GST loader (which follows gst_pico limit of 2)
        assert len(results) == 1


def test_load_dataset_gst_full_slice():
    """Test loading full GST slice (76 questions)."""
    config = {"datasets": {"gst_legal_rag": {"cache_path": "test_cache"}}}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_dir = tmp_path / "test_cache"
        cache_dir.mkdir(parents=True)

        # Create mock questions.jsonl with 76 questions
        questions_file = cache_dir / "questions.jsonl"
        lines = [
            f'{{"id": "{i}", "question": "Q{i}", "answer": "A{i}", "relevant_passage_id": "P{i}"}}'
            for i in range(1, 77)
        ]
        questions_file.write_text("\n".join(lines) + "\n")

        # Override cache path in config
        config["datasets"]["gst_legal_rag"]["cache_path"] = str(cache_dir)
        results = list(load_dataset("gst_full", config))

        assert len(results) == 76


def test_load_dataset_gst_nano_slice():
    """Test loading nano GST slice (10 questions)."""
    config = {"datasets": {"gst_legal_rag": {"cache_path": "test_cache"}}}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_dir = tmp_path / "test_cache"
        cache_dir.mkdir(parents=True)

        # Create mock questions.jsonl with more than 10 questions
        questions_file = cache_dir / "questions.jsonl"
        lines = [
            f'{{"id": "{i}", "question": "Q{i}", "answer": "A{i}", "relevant_passage_id": "P{i}"}}'
            for i in range(1, 21)
        ]
        questions_file.write_text("\n".join(lines) + "\n")

        # Override cache path in config
        config["datasets"]["gst_legal_rag"]["cache_path"] = str(cache_dir)
        results = list(load_dataset("gst_nano", config))

        assert len(results) == 10


def test_load_dataset_gst_mini_slice():
    """Test loading mini GST slice (20 questions)."""
    config = {"datasets": {"gst_legal_rag": {"cache_path": "test_cache"}}}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_dir = tmp_path / "test_cache"
        cache_dir.mkdir(parents=True)

        # Create mock questions.jsonl with more than 20 questions
        questions_file = cache_dir / "questions.jsonl"
        lines = [
            f'{{"id": "{i}", "question": "Q{i}", "answer": "A{i}", "relevant_passage_id": "P{i}"}}'
            for i in range(1, 31)
        ]
        questions_file.write_text("\n".join(lines) + "\n")

        # Override cache path in config
        config["datasets"]["gst_legal_rag"]["cache_path"] = str(cache_dir)
        results = list(load_dataset("gst_mini", config))

        assert len(results) == 20


# ---------------------------------------------------------------------------
# get_rag registration seam (lazy imports; no live/network calls)
# ---------------------------------------------------------------------------


def test_stub_local_registers_without_opensearch_extra(monkeypatch):
    """--rag stub-local still builds a RagAdapter with opensearchpy absent."""
    import sys
    from unittest.mock import MagicMock

    # Simulate the opensearch extra being uninstalled: any `import opensearchpy`
    # during registration would raise ImportError.
    monkeypatch.setitem(sys.modules, "opensearchpy", None)

    # stub-local's chromadb dep may not be installed in this env; the test
    # targets get_rag's lazy-import seam, not chromadb itself.
    injected_chromadb = []
    for mod in ("chromadb", "chromadb.utils"):
        if mod not in sys.modules:
            monkeypatch.setitem(sys.modules, mod, MagicMock())
            injected_chromadb.append(mod)

    from app.kernel.interfaces import RagAdapter
    from dev.cli.run_rag_eval import get_rag

    try:
        adapter = get_rag("stub-local", top_k=3)
        assert isinstance(adapter, RagAdapter)
    finally:
        # Drop any dev modules that were first imported against the mocked
        # chromadb so later tests never see mock-bound module state.
        if injected_chromadb:
            sys.modules.pop("dev.stubs.rag.chromadb_query", None)
            sys.modules.pop("dev.stubs.rag.chromadb_client", None)


def test_opensearch_registers_lazily(monkeypatch):
    """--rag opensearch builds a RagAdapter without touching opensearchpy at
    registration time (the client import is deferred to query time)."""
    import sys

    monkeypatch.setitem(sys.modules, "opensearchpy", None)

    from app.kernel.interfaces import RagAdapter
    from dev.cli.run_rag_eval import get_rag

    adapter = get_rag("opensearch", top_k=5)
    assert isinstance(adapter, RagAdapter)


def test_build_embedder_defaults_to_bedrock_for_opensearch():
    """--rag opensearch defaults the shared embedder to the Titan (bedrock) class."""
    from dev.cli.run_rag_eval import _build_embedder
    from dev.stubs.rag.bedrock_embedder import BedrockTitanEmbedder

    def _unused_get_embedder(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("bedrock provider must not route through app get_embedder")

    embedder = _build_embedder({}, _unused_get_embedder, rag_name="opensearch")
    assert isinstance(embedder, BedrockTitanEmbedder)


def test_build_embedder_keeps_huggingface_for_stub_local():
    """stub-local keeps the sentence-transformers (huggingface) provider default."""
    from dev.cli.run_rag_eval import _build_embedder

    seen = {}

    def _fake_get_embedder(provider, model):
        seen["provider"] = provider
        seen["model"] = model
        return object()

    _build_embedder({}, _fake_get_embedder, rag_name="stub-local")
    assert seen["provider"] == "huggingface"


# ---------------------------------------------------------------------------
# --rag orchestrator (HTTP backend; no live orchestrator, no network)
# ---------------------------------------------------------------------------


def test_orchestrator_registers_with_no_query_side_embedder():
    """--rag orchestrator builds a RagAdapter and _build_embedder yields None."""
    from app.kernel.interfaces import RagAdapter
    from dev.cli.run_rag_eval import _build_embedder, get_rag

    def _unused_get_embedder(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("orchestrator must not build a query-side embedder")

    # The orchestrator embeds server-side: no huggingface/bedrock embedder is
    # built (a sentence-transformers download here would be a regression).
    assert _build_embedder({}, _unused_get_embedder, rag_name="orchestrator") is None

    adapter = get_rag("orchestrator", embedder=None)
    assert isinstance(adapter, RagAdapter)


def test_top_k_with_orchestrator_fails_loudly(capsys):
    """BINDING (spec Q11): --top-k + --rag orchestrator is an error, never a no-op."""
    import pytest
    from dev.cli.run_rag_eval import _build_args

    with pytest.raises(SystemExit) as exc_info:
        _build_args(["--rag", "orchestrator", "--top-k", "5"])

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "pipeline config owns top_k" in err
    assert "ORCHESTRATOR_PIPELINE_CONFIG" in err

    # Without an explicit --top-k the orchestrator backend parses fine (the
    # guard fires only on an EXPLICITLY provided flag, never the default).
    args = _build_args(["--rag", "orchestrator"])
    assert args.rag == "orchestrator"
    assert args.top_k is None
