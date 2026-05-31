"""
Phoenix dataset management for Legal RAG Bench.

Converts Legal RAG Bench dataset to Phoenix dataset format for use with
run_experiment() API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from beartype import beartype

try:
    from phoenix.client import Client
    from phoenix.client.resources.datasets import Dataset
except ImportError:
    Client = None  # type: ignore[assignment]
    Dataset = None  # type: ignore[assignment]

# Constants
DEFAULT_DATASET_NAME: Final[str] = "legal-rag-bench"
DEFAULT_GST_DATASET_NAME: Final[str] = "gst-legal-rag"


@beartype
def create_phoenix_dataset(
    client: Client,
    corpus_dir: Path,
    slice_name: str = "pico",
    dataset_name: str | None = None,
) -> Dataset:
    """
    Create or get a Phoenix dataset from Legal RAG Bench or GST Legal RAG.

    Routes to appropriate loader based on slice_name prefix:
    - gst_* slices use GST Legal RAG loader
    - other slices use Legal RAG Bench loader

    Args:
        client: Phoenix client instance.
        corpus_dir: Path to corpus directory.
        slice_name: Dataset slice (e.g., "pico", "nano", "gst_pico", "gst_full").
        dataset_name: Name for the dataset (auto-generated if None).

    Returns:
        Phoenix Dataset instance.

    Raises:
        ImportError: If Phoenix client is not available.
        ValueError: If corpus_dir does not exist.

    """
    if Client is None:
        raise ImportError("Phoenix client not available")

    if not corpus_dir.exists():
        raise ValueError(f"Corpus directory does not exist: {corpus_dir}")

    # Route to appropriate loader based on slice prefix
    if slice_name.startswith("gst_"):
        from crucible.datasets import load_gst_legal_rag

        load_fn = load_gst_legal_rag
        base_name = DEFAULT_GST_DATASET_NAME
    else:
        from crucible.datasets import load_legal_rag_bench

        load_fn = load_legal_rag_bench
        base_name = DEFAULT_DATASET_NAME

    # Load dataset
    dataset = load_fn(cache_dir=corpus_dir, slice=slice_name)

    # Convert to Phoenix format
    # Phoenix internally maps dataset outputs to evaluator's 'expected' parameter
    # So we use 'expected' key in outputs
    inputs = []
    outputs = []
    metadata_list = []

    for query_id, query_text, relevant_passage_id, gold_answer in dataset:
        inputs.append({"input": query_text})
        outputs.append({"expected": gold_answer})
        metadata_list.append(
            {
                "query_id": query_id,
                "relevant_passage_id": relevant_passage_id,
            }
        )

    # Create or get dataset in Phoenix
    name = dataset_name or f"{base_name}-{slice_name}"

    # Try to get existing dataset first
    try:
        existing = client.datasets.get_dataset(dataset=name)
        # Return existing without adding - prevents duplicates on repeated runs
        return existing
    except Exception:
        # Dataset doesn't exist, create new one
        return client.datasets.create_dataset(
            name=name,
            inputs=inputs,
            outputs=outputs,
            metadata=metadata_list,
            input_keys=["input"],
            output_keys=["expected"],
            dataset_description=f"{base_name} {slice_name} slice",
        )


@beartype
def get_phoenix_dataset(
    client: Client,
    slice_name: str = "pico",
) -> Dataset | None:
    """
    Get an existing Phoenix dataset by name.

    Args:
        client: Phoenix client instance.
        slice_name: Dataset slice (e.g., "pico", "nano", "gst_pico", "gst_full").

    Returns:
        Phoenix Dataset instance or None if not found.

    """
    if Client is None:
        return None

    # Route to appropriate base name based on slice prefix
    if slice_name.startswith("gst_"):
        base_name = DEFAULT_GST_DATASET_NAME
    else:
        base_name = DEFAULT_DATASET_NAME

    name = f"{base_name}-{slice_name}"

    try:
        return client.datasets.get_dataset(dataset=name)
    except Exception:
        return None
