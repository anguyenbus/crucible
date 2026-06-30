"""CSV writer for evaluation results."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from app.runners.golden_set import RunResult


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


def write_run_result(result: RunResult, output_path: Path) -> None:
    """
    Persist a golden-set ``RunResult`` (collect-then-write) to CSV.

    The service library collects the per-query rows into a pure ``RunResult``;
    this shell-side writer flattens the typed rows to the wide golden-set CSV
    schema and writes once (replacing the old row-by-row CSV flush).

    Args:
        result: The ``RunResult`` returned by ``run_golden_set``.
        output_path: Path where the CSV should be written.

    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "query_id",
        "question",
        "gold_answer",
        "generated_answer",
        "relevant_passage_retrieved",
        "faithfulness_score",
        "context_precision_score",
        "context_recall_score",
        "answer_relevancy_score",
        "judge_verdict",
        "total_ms",
        "error",
    ]
    rows = [
        {
            "query_id": r.query_id,
            "question": r.question,
            "gold_answer": r.gold_answer,
            "generated_answer": r.generated_answer,
            "relevant_passage_retrieved": r.relevant_passage_retrieved,
            "faithfulness_score": r.faithfulness_score,
            "context_precision_score": r.context_precision_score,
            "context_recall_score": r.context_recall_score,
            "answer_relevancy_score": r.answer_relevancy_score,
            "judge_verdict": r.judge_verdict,
            "total_ms": r.total_ms,
            "error": r.error,
        }
        for r in result.rows
    ]
    df = pd.DataFrame(rows, columns=columns)
    df.to_csv(output_path, index=False)
