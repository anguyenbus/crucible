"""CSV writer for evaluation results."""

from pathlib import Path
from typing import Any

import pandas as pd


def write_results(results: list[dict[str, Any]], output_path: Path) -> None:
    """
    Write evaluation results to CSV file.

    Args:
        results: List of result dictionaries with keys:
            - query_id: Identifier for the query/document
            - question_id: Identifier for the evaluation question
            - score: Numeric score
            - label: Pass/fail label
            - error: Error message if any
        output_path: Path where CSV file should be written.

    Example:
        >>> results = [
        ...     {"query_id": "q001", "question_id": "f1", "score": 0.95,
        ...      "label": "pass", "error": ""},
        ...     {"query_id": "q002", "question_id": "recall", "score": 0.80,
        ...      "label": "fail", "error": ""}
        ... ]
        >>> write_results(results, Path("results.csv"))

    """
    # Create output directory if needed
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Define column order
    columns = ["query_id", "question_id", "score", "label", "error"]

    # Create DataFrame and write to CSV
    df = pd.DataFrame(results, columns=columns)
    df.to_csv(output_path, index=False)
