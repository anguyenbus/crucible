"""Tests for RAG eval CLI integration and dataset resolver."""

from pathlib import Path
import tempfile

import pytest

from crucible.runners.run_rag_eval import load_dataset


def test_load_dataset_routes_gst_prefix():
    """Test that gst_* prefix routes to GST loader."""
    # Create mock config with gst_legal_rag
    config = {
        "datasets": {
            "gst_legal_rag": {
                "cache_path": "test_cache"
            }
        }
    }

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
    from unittest.mock import patch

    config = {
        "datasets": {
            "gst_legal_rag": {
                "cache_path": "test_cache"
            }
        }
    }

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
    config = {
        "datasets": {
            "gst_legal_rag": {
                "cache_path": "test_cache"
            }
        }
    }

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
    config = {
        "datasets": {
            "gst_legal_rag": {
                "cache_path": "test_cache"
            }
        }
    }

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
    config = {
        "datasets": {
            "gst_legal_rag": {
                "cache_path": "test_cache"
            }
        }
    }

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
