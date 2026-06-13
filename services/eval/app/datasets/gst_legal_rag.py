"""
GST Legal RAG dataset loader.

This module loads the GST (Australian Goods and Services Tax) legal RAG dataset
from vendored local JSONL files for RAG evaluation with LLM-judge metrics.

Dataset: GST Legal RAG (vendored in repository)
- questions.jsonl: 76 questions with reference answers and relevant_passage_id
- documents.jsonl: 5,263 passages from Australian GST legislation
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from beartype import beartype
from beartype.typing import Optional

# Constants
DATASET_NAME: Final[str] = "gst-legal-rag"

# Slice sizes
SLICE_PICO: Final[int] = 2
SLICE_NANO: Final[int] = 10
SLICE_MINI: Final[int] = 20
SLICE_FULL: Final[int] = 76


@beartype
def _get_gst_slice_limit(slice_name: str) -> Optional[int]:
    """
    Get the number of questions for a given GST slice.

    Args:
        slice_name: GST slice name with "gst_" prefix (e.g., "gst_pico").

    Returns:
        Number of questions to yield for the slice.

    Raises:
        ValueError: If slice_name does not start with "gst_" or is not valid.

    """
    if not slice_name.startswith("gst_"):
        raise ValueError("GST slice must be one of: gst_full, gst_mini, gst_nano, gst_pico")

    suffix = slice_name.removeprefix("gst_")

    if suffix == "pico":
        return SLICE_PICO
    if suffix == "nano":
        return SLICE_NANO
    if suffix == "mini":
        return SLICE_MINI
    if suffix == "full":
        return SLICE_FULL

    raise ValueError("GST slice must be one of: gst_full, gst_mini, gst_nano, gst_pico")


@beartype
def _ensure_cache_dir(cache_dir: Path) -> None:
    """
    Ensure cache directory exists.

    Args:
        cache_dir: Path to cache directory.

    """
    cache_dir.mkdir(parents=True, exist_ok=True)


@beartype
def load_gst_legal_rag(
    cache_dir: Path,
    slice: str = "gst_full",
    force_refresh: bool = False,
) -> Iterator[tuple[str, str, str, str]]:
    """
    Load GST Legal RAG dataset and yield query tuples.

    Loads the GST dataset from vendored local JSONL files. Yields tuples of
    (query_id, query_text, relevant_passage_id, reference_answer).

    Args:
        cache_dir: Path to local cache directory containing questions.jsonl
            (supplied by the caller/config; no library-reachable default).
        slice: GST slice name. Options: "gst_pico" (2), "gst_nano" (10),
            "gst_mini" (20), "gst_full" (76). Default: "gst_full".
        force_refresh: Accepted but ignored. Local files have nothing to re-download.

    Yields:
        tuple: (query_id, query_text, relevant_passage_id, reference_answer) where:
            - query_id: Unique query identifier (cast to string)
            - query_text: The question text
            - relevant_passage_id: Single passage ID containing answer
            - reference_answer: The reference answer text

    Raises:
        ValueError: If slice is not a valid GST slice name.
        FileNotFoundError: If questions.jsonl does not exist in cache_dir.

    Example:
        >>> for query_id, query_text, passage_id, answer in load_gst_legal_rag():
        ...     print(f"{query_id}: {query_text}")

    """
    # Validate slice
    limit = _get_gst_slice_limit(slice)

    # Ensure cache directory exists
    _ensure_cache_dir(cache_dir)

    # Path to questions.jsonl
    questions_path = cache_dir / "questions.jsonl"

    if not questions_path.exists():
        raise FileNotFoundError(
            f"questions.jsonl not found in {cache_dir}. "
            f"Please ensure the GST dataset files are vendored correctly."
        )

    # Read and parse questions.jsonl
    with questions_path.open(encoding="utf-8") as f:
        count = 0
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                item = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in questions.jsonl: {e}") from e

            # Extract fields, using empty string as fallback
            query_id = str(item.get("id", ""))
            query_text = item.get("question", "")
            reference_answer = item.get("answer", "")
            relevant_passage_id = item.get("relevant_passage_id", "")

            yield (query_id, query_text, relevant_passage_id, reference_answer)
            count += 1

            # Check limit
            if limit is not None and count >= limit:
                break
