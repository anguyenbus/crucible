"""Tests for Legal RAG Bench dataset loader."""

from pathlib import Path
import tempfile

import pytest

from crucible.datasets.legal_rag_bench import (
    DATASET_NAME,
    HF_TOKEN_ENV,
    SLICE_NANO,
    SLICE_PICO,
    _get_hf_token,
    _get_slice_limit,
)


def test_constants():
    """Test module constants."""
    assert DATASET_NAME == "isaacus/legal-rag-bench"
    assert HF_TOKEN_ENV == "HF_TOKEN"
    assert SLICE_PICO == 2
    assert SLICE_NANO == 10


def test_get_hf_token_from_env():
    """Test HF token resolution from environment variable."""
    import os

    # Set environment variable
    os.environ["HF_TOKEN"] = "test-token"
    token = _get_hf_token()
    assert token == "test-token"
    del os.environ["HF_TOKEN"]


def test_get_hf_token_from_file(tmp_path: Path):
    """Test HF token resolution from file."""
    import os

    # Create temporary token file
    hf_dir = tmp_path / ".huggingface"
    hf_dir.mkdir(parents=True)
    token_file = hf_dir / "token"
    token_file.write_text("file-token")

    # Temporarily replace default path
    from crucible.datasets import legal_rag_bench
    original_path = legal_rag_bench.DEFAULT_HF_TOKEN_PATH
    legal_rag_bench.DEFAULT_HF_TOKEN_PATH = token_file

    try:
        token = _get_hf_token()
        assert token == "file-token"
    finally:
        legal_rag_bench.DEFAULT_HF_TOKEN_PATH = original_path


def test_get_hf_token_not_found():
    """Test HF token resolution when not found."""
    import os

    # Ensure env var is not set
    if "HF_TOKEN" in os.environ:
        del os.environ["HF_TOKEN"]

    # Mock home directory to avoid real token file
    from crucible.datasets import legal_rag_bench
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        original_path = legal_rag_bench.DEFAULT_HF_TOKEN_PATH
        legal_rag_bench.DEFAULT_HF_TOKEN_PATH = Path(tmp) / ".huggingface" / "token"

        try:
            token = _get_hf_token()
            assert token is None
        finally:
            legal_rag_bench.DEFAULT_HF_TOKEN_PATH = original_path


def test_get_slice_limit_pico():
    """Test pico slice limit."""
    limit = _get_slice_limit("pico")
    assert limit == SLICE_PICO
    assert limit == 2


def test_get_slice_limit_nano():
    """Test nano slice limit."""
    limit = _get_slice_limit("nano")
    assert limit == SLICE_NANO
    assert limit == 10


def test_get_slice_limit_full():
    """Test full slice (no limit)."""
    limit = _get_slice_limit("full")
    assert limit is None


def test_get_slice_limit_invalid():
    """Test invalid slice name."""
    with pytest.raises(ValueError, match="slice must be"):
        _get_slice_limit("invalid")


def test_ensure_cache_dir(tmp_path: Path):
    """Test cache directory creation."""
    from crucible.datasets.legal_rag_bench import _ensure_cache_dir

    cache_dir = tmp_path / "cache" / "nested"
    _ensure_cache_dir(cache_dir)

    assert cache_dir.exists()
    assert cache_dir.is_dir()
