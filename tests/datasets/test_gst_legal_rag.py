"""Tests for GST Legal RAG dataset loader."""

from pathlib import Path
import tempfile

import pytest

from crucible.datasets.gst_legal_rag import (
    DATASET_NAME,
    DEFAULT_CACHE_DIR,
    SLICE_FULL,
    SLICE_MINI,
    SLICE_NANO,
    SLICE_PICO,
    _get_gst_slice_limit,
    _ensure_cache_dir,
    load_gst_legal_rag,
)


def test_constants():
    """Test module constants."""
    assert DATASET_NAME == "gst-legal-rag"
    assert DEFAULT_CACHE_DIR == Path("data/rag/gst_legal_rag/")
    assert SLICE_PICO == 2
    assert SLICE_NANO == 10
    assert SLICE_MINI == 20
    assert SLICE_FULL == 76


def test_get_gst_slice_limit_pico():
    """Test pico slice limit."""
    limit = _get_gst_slice_limit("gst_pico")
    assert limit == SLICE_PICO
    assert limit == 2


def test_get_gst_slice_limit_nano():
    """Test nano slice limit."""
    limit = _get_gst_slice_limit("gst_nano")
    assert limit == SLICE_NANO
    assert limit == 10


def test_get_gst_slice_limit_mini():
    """Test mini slice limit."""
    limit = _get_gst_slice_limit("gst_mini")
    assert limit == SLICE_MINI
    assert limit == 20


def test_get_gst_slice_limit_full():
    """Test full slice (no limit)."""
    limit = _get_gst_slice_limit("gst_full")
    assert limit == SLICE_FULL
    assert limit == 76


def test_get_gst_slice_limit_invalid():
    """Test invalid GST slice name raises ValueError."""
    with pytest.raises(ValueError, match="GST slice must be one of"):
        _get_gst_slice_limit("gst_invalid")


def test_get_gst_slice_limit_non_gst_prefix():
    """Test non-gst prefix raises ValueError."""
    with pytest.raises(ValueError, match="GST slice must be one of"):
        _get_gst_slice_limit("pico")


def test_ensure_cache_dir(tmp_path: Path):
    """Test cache directory creation."""
    cache_dir = tmp_path / "cache" / "nested"
    _ensure_cache_dir(cache_dir)

    assert cache_dir.exists()
    assert cache_dir.is_dir()


def test_load_gst_legal_rag_pico_slice(tmp_path: Path):
    """Test loading pico slice (2 questions)."""
    # Create mock questions.jsonl
    questions_file = tmp_path / "questions.jsonl"
    questions_file.write_text(
        '{"id": "1", "question": "Q1", "answer": "A1", "relevant_passage_id": "P1"}\n'
        '{"id": "2", "question": "Q2", "answer": "A2", "relevant_passage_id": "P2"}\n'
        '{"id": "3", "question": "Q3", "answer": "A3", "relevant_passage_id": "P3"}\n'
    )

    # Load pico slice (should return 2)
    results = list(load_gst_legal_rag(cache_dir=tmp_path, slice="gst_pico"))

    assert len(results) == 2
    assert results[0] == ("1", "Q1", "P1", "A1")
    assert results[1] == ("2", "Q2", "P2", "A2")


def test_load_gst_legal_rag_id_casting(tmp_path: Path):
    """Test that id field is properly cast to string."""
    questions_file = tmp_path / "questions.jsonl"
    # Use numeric id to test casting
    questions_file.write_text(
        '{"id": 42, "question": "Q", "answer": "A", "relevant_passage_id": "P"}\n'
    )

    results = list(load_gst_legal_rag(cache_dir=tmp_path, slice="gst_full"))

    assert len(results) == 1
    assert results[0][0] == "42"  # id should be cast to string
