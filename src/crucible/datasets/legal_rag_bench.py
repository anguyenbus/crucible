"""
Legal RAG Bench dataset loader.

This module loads the Legal RAG Benchmark dataset from HuggingFace
for RAG evaluation with LLM-judge metrics. Supports both cached and
uncached loading modes.

Dataset: isaacus/legal-rag-bench
- test split: 100 questions with reference answers and relevant_passage_id
- corpus split: 4,876 passages from Victorian Criminal Charge Book
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from beartype import beartype
from beartype.typing import Optional

# Constants
DATASET_NAME: Final[str] = "isaacus/legal-rag-bench"
HF_TOKEN_ENV: Final[str] = "HF_TOKEN"
DEFAULT_HF_TOKEN_PATH: Final[Path] = Path.home() / ".huggingface" / "token"
DEFAULT_CACHE_DIR: Final[Path] = Path("data/rag/legal_rag_bench/")

# Slice sizes
SLICE_PICO: Final[int] = 2
SLICE_NANO: Final[int] = 10


@beartype
def _get_hf_token() -> Optional[str]:
    """
    Get HuggingFace token from environment or default file path.

    Checks environment variable HF_TOKEN first, then falls back to
    ~/.huggingface/token file.

    Returns:
        HuggingFace token string, or None if not found.

    """
    # Check environment variable first
    token = os.environ.get(HF_TOKEN_ENV)
    if token:
        return token

    # Check default file path
    if DEFAULT_HF_TOKEN_PATH.exists():
        return DEFAULT_HF_TOKEN_PATH.read_text().strip()

    return None


@beartype
def _ensure_cache_dir(cache_dir: Path) -> None:
    """
    Ensure cache directory exists.

    Args:
        cache_dir: Path to cache directory.

    """
    cache_dir.mkdir(parents=True, exist_ok=True)


@beartype
def _get_slice_limit(slice_name: str) -> Optional[int]:
    """
    Get the number of questions for a given slice.

    Args:
        slice_name: Either "pico" (2), "nano" (10), or "full" (100 questions).

    Returns:
        Number of questions to yield, or None for full dataset.

    Raises:
        ValueError: If slice_name is not valid.

    """
    if slice_name == "pico":
        return SLICE_PICO
    if slice_name == "nano":
        return SLICE_NANO
    elif slice_name == "full":
        return None  # No limit - all 100 questions
    else:
        raise ValueError(f"slice must be 'pico', 'nano' or 'full', got: {slice_name}")


@beartype
def load_legal_rag_bench(
    cache_dir: Path = DEFAULT_CACHE_DIR,
    slice: str = "full",
    force_refresh: bool = False,
) -> Iterator[tuple[str, str, str, str]]:
    """
    Load Legal RAG Bench dataset and yield query tuples.

    Loads the isaacus/legal-rag-bench dataset from HuggingFace with
    optional local caching. Yields tuples of (query_id, query_text,
    relevant_passage_id, reference_answer).

    Args:
        cache_dir: Path to local cache directory. Default: data/rag/legal_rag_bench/.
        slice: Either "pico" (2), "nano" (10), or "full" (100). Default: "full".
        force_refresh: If True, skip cache and re-download. Default: False.

    Yields:
        tuple: (query_id, query_text, relevant_passage_id, reference_answer) where:
            - query_id: Unique query identifier
            - query_text: The question text
            - relevant_passage_id: Single passage ID containing answer
            - reference_answer: The reference answer text

    Raises:
        ValueError: If slice is not "pico", "nano" or "full".
        FileNotFoundError: If HF token is required but not found.

    Example:
        >>> for query_id, query_text, passage_id, answer in load_legal_rag_bench():
        ...     print(f"{query_id}: {query_text}")

    """
    # Validate slice
    limit = _get_slice_limit(slice)

    # Ensure cache directory exists
    _ensure_cache_dir(cache_dir)

    # Import here to avoid dependency if not used
    try:
        from datasets import load_dataset as hf_load_dataset
    except ImportError as err:
        raise ImportError(
            "datasets library not installed. Install with: uv add datasets"
        ) from err

    # Get HF token
    token = _get_hf_token()

    # Load dataset from HuggingFace
    # The dataset has two subsets: "corpus" (documents) and "qa" (questions)
    # We need the "qa" subset for evaluation
    try:
        dataset = hf_load_dataset(
            DATASET_NAME,
            name="qa",  # Load QA subset, not corpus
            split="test",
            token=token,
            cache_dir=str(cache_dir) if cache_dir else None,
        )
    except Exception as e:
        if "repoids" in str(e).lower() or "gated" in str(e).lower():
            raise FileNotFoundError(
                f"Cannot access {DATASET_NAME}. Please set HF_TOKEN environment "
                f"variable or ensure ~/.huggingface/token exists."
            ) from e
        raise

    # Yield tuples up to limit
    count = 0
    for item in dataset:
        # Map dataset fields: has id, question, answer, relevant_passage_id
        query_text = item.get("question", "")
        reference_answer = item.get("answer", "")
        relevant_passage_id = item.get("relevant_passage_id", "")
        query_id = item.get("id", f"q_{count}")

        yield (query_id, query_text, relevant_passage_id, reference_answer)
        count += 1

        # Check limit
        if limit is not None and count >= limit:
            break
