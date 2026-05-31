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


@beartype
def create_phoenix_dataset(
    client: Client,
    corpus_dir: Path,
    slice_name: str = "pico",
    dataset_name: str | None = None,
) -> Dataset:
    """
    Create or get a Phoenix dataset from Legal RAG Bench.

    Args:
        client: Phoenix client instance.
        corpus_dir: Path to Legal RAG Bench corpus.
        slice_name: Dataset slice ("pico", "nano", or "full").
        dataset_name: Name for the dataset (defaults to "legal-rag-bench-{slice}").

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

    from crucible.datasets import load_legal_rag_bench

    # Load Legal RAG Bench dataset
    dataset = load_legal_rag_bench(cache_dir=corpus_dir, slice=slice_name)

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
    name = dataset_name or f"{DEFAULT_DATASET_NAME}-{slice_name}"

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
            dataset_description=f"Legal RAG Bench {slice_name} slice",
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
        slice_name: Dataset slice ("pico", "nano", or "full").

    Returns:
        Phoenix Dataset instance or None if not found.

    """
    if Client is None:
        return None

    name = f"{DEFAULT_DATASET_NAME}-{slice_name}"

    try:
        return client.datasets.get_dataset(dataset=name)
    except Exception:
        return None
