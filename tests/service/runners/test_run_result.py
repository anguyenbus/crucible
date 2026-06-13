"""
Focused unit tests for the ``RunResult`` shape contract (Task Group 6.1).

These tests pin the PURE-dataclass shape of ``RunResult`` and the no-file-I/O
boundary of ``run_golden_set`` -- the shape contract only, not exhaustive
aggregate coverage. ``run_golden_set`` collects rows + a summary and returns
them; the CLI shell (not the library) persists CSV/Parquet.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from crucible.kernel.interfaces import RagAdapter
from crucible.service.runners.golden_set import (
    QueryRow,
    RunResult,
    RunSummary,
    run_golden_set,
)


def _fake_rag_output(answer: str = "ans") -> dict:
    """A schema-conformant rag_query_output dict (so RagAdapter.query validates)."""
    return {
        "schema_version": "1.0.0",
        "system_version": {"pipeline_version": "1.0.0"},
        "query": {"query_id": "q1", "text": "What is X?"},
        "answer": {"text": answer, "citations": []},
        "retrieved_chunks": [],
        "timings_ms": {"total": 5},
    }


class _StubEvaluator:
    """Minimal evaluator stand-in matching DeepEvalEvaluator's surface."""

    def compute_metrics_with_reasoning(self, output: dict, gold_answer: str) -> dict:
        return {
            "scores": {
                "faithfulness": 0.9,
                "context_precision": 0.8,
                "context_recall": 0.7,
                "answer_relevancy": 0.6,
            },
            "reasoning": {"faithfulness": "looks grounded"},
        }


def _adapter() -> RagAdapter:
    def _query(question: str, corpus_dir: Path) -> dict:
        return _fake_rag_output()

    return RagAdapter(query_callable=_query)


def test_runresult_has_slots_and_is_immutable_shape() -> None:
    rows = (QueryRow(query_id="q1", question="?", gold_answer="g", generated_answer="a"),)
    summary = RunSummary(success_count=1, error_count=0, num_queries=1)
    result = RunResult(rows=rows, summary=summary)

    assert hasattr(RunResult, "__slots__")
    assert hasattr(QueryRow, "__slots__")
    assert hasattr(RunSummary, "__slots__")
    # __slots__ + frozen means a stray attribute cannot be set.
    with pytest.raises((AttributeError, TypeError)):
        result.unexpected = 1  # type: ignore[attr-defined]


def test_run_golden_set_collects_typed_rows_and_summary() -> None:
    dataset = [
        ("q1", "What is X?", "p1", "gold-1"),
        ("q2", "What is Y?", "p2", "gold-2"),
    ]
    result = run_golden_set(
        dataset=dataset,
        adapter=_adapter(),
        evaluator=_StubEvaluator(),
        config={},
    )

    assert isinstance(result, RunResult)
    assert result.summary.success_count == 2
    assert result.summary.error_count == 0
    assert result.summary.num_queries == 2
    assert len(result.rows) == 2
    first = result.rows[0]
    assert isinstance(first, QueryRow)
    assert first.query_id == "q1"
    assert first.faithfulness_score == pytest.approx(0.9)
    assert first.judge_verdict == "PASS"
    # metric aggregates are exposed on the summary
    assert "faithfulness" in result.summary.metric_means


def test_run_golden_set_records_errors_without_raising() -> None:
    def _boom_query(question: str, corpus_dir: Path) -> dict:
        raise RuntimeError("boom")

    dataset = [("q1", "Q?", "p1", "g1")]
    result = run_golden_set(
        dataset=dataset,
        adapter=RagAdapter(query_callable=_boom_query),
        evaluator=_StubEvaluator(),
        config={},
    )
    assert result.summary.error_count == 1
    assert result.summary.success_count == 0
    assert result.rows[0].error != ""
    assert result.rows[0].judge_verdict == "ERROR"


def test_run_golden_set_has_no_file_io_in_source() -> None:
    """The service library must not reach for results/ or data/ paths in code."""
    src = inspect.getsource(run_golden_set)
    # Strip the docstring so the assertions inspect executable code only.
    doc = run_golden_set.__doc__ or ""
    code = src.replace(doc, "")
    assert 'Path("results/' not in code
    assert 'Path("data/' not in code
    assert "open(" not in code
    assert ".to_csv(" not in code
