"""Tests for GST loader exports and config loading."""

import tempfile
from pathlib import Path

from app import datasets
from app.config import load_config


def test_gst_loader_accessible_from_datasets():
    """Test that GST loader is accessible from app.datasets."""
    assert hasattr(datasets, "load_gst_legal_rag")
    assert callable(datasets.load_gst_legal_rag)


def test_gst_constants_exported():
    """Test that GST constants are exported."""
    assert hasattr(datasets, "GST_DATASET_NAME")
    assert hasattr(datasets, "GST_SLICE_PICO")
    assert hasattr(datasets, "GST_SLICE_NANO")
    assert hasattr(datasets, "GST_SLICE_MINI")
    assert hasattr(datasets, "GST_SLICE_FULL")


def test_gst_constants_values():
    """Test GST constant values."""
    assert datasets.GST_DATASET_NAME == "gst-legal-rag"
    assert datasets.GST_SLICE_PICO == 2
    assert datasets.GST_SLICE_NANO == 10
    assert datasets.GST_SLICE_MINI == 20
    assert datasets.GST_SLICE_FULL == 76


# eval_config.yaml lives in the dev fixtures (services/eval/dev/fixtures/).
_EVAL_CONFIG_PATH = Path(__file__).resolve().parents[2] / "dev" / "fixtures" / "eval_config.yaml"


def test_eval_config_has_gst_block():
    """Test that eval_config.yaml contains gst_legal_rag configuration."""
    config = load_config(_EVAL_CONFIG_PATH)

    assert "datasets" in config
    assert "gst_legal_rag" in config["datasets"]

    gst_config = config["datasets"]["gst_legal_rag"]
    assert gst_config["path"] == "data/rag/gst_legal_rag"
    assert gst_config["cache_path"] == "data/rag/gst_legal_rag"
    assert "embeddings" in gst_config
    # The judge ("deepeval") config is no longer per-dataset: it now lives in the
    # top-level global `judge:` block (see eval_config.yaml / get_deepeval_config).


def test_gst_in_all_export_list():
    """Test that GST exports are in __all__ list."""
    from app.datasets import __all__

    expected_exports = [
        "load_gst_legal_rag",
        "GST_DATASET_NAME",
        "GST_SLICE_PICO",
        "GST_SLICE_NANO",
        "GST_SLICE_MINI",
        "GST_SLICE_FULL",
    ]

    for export in expected_exports:
        assert export in __all__, f"{export} not in __all__"


def test_load_gst_legal_rag_from_export():
    """Test loading GST data through exported module."""
    from app.datasets import load_gst_legal_rag

    # Create mock questions.jsonl
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        questions_file = tmp_path / "questions.jsonl"
        questions_file.write_text(
            '{"id": "1", "question": "Q", "answer": "A", "relevant_passage_id": "P"}\n'
        )

        results = list(load_gst_legal_rag(cache_dir=tmp_path, slice="gst_full"))

        assert len(results) == 1
        assert results[0] == ("1", "Q", "P", "A")
