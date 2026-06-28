"""
Golden-set evaluation library (pure, dependency-injected).

Phase 3 extracts the golden-set loop out of the ``__main__`` CLI shell into two
injectable library functions:

- ``run_golden_set(...) -> RunResult`` -- the CSV/``RunResult`` engine. It iterates
  the dataset, queries the injected ``RagAdapter``, computes metrics via the
  injected evaluator, and COLLECTS the per-query rows + a summary into a pure
  ``RunResult`` dataclass. It performs NO file I/O: there is no
  ``Path("results/...")`` / ``Path("data/...")`` reachable from this module. It is
  retained as an importable engine (tests + the reserved replay engine); it has no
  Phoenix wiring.
- ``run_phoenix_native(...)`` -- the Phoenix-native Datasets & Experiments path
  (the ONLY Phoenix path). It has a different dependency set + return shape and
  delegates to ``crucible.service.phoenix.experiments``.

All infra (evaluator, RAG adapter, metrics) is BUILT IN THE SHELL and injected;
the library never builds boto/phoenix clients itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from beartype import beartype
from beartype.typing import Protocol

from crucible.kernel.interfaces import ClaimStore, RagAdapter

# Faithfulness threshold for the PASS / NEEDS_REVIEW verdict (pinned from the
# former run_rag_eval shell; the shell behavior is preserved this phase).
_FAITHFULNESS_PASS_THRESHOLD: Final[float] = 0.7


class _Evaluator(Protocol):
    """Structural surface of the injected evaluator (kernel DeepEvalEvaluator)."""

    def compute_metrics_with_reasoning(
        self, output: dict[str, Any], gold_answer: str
    ) -> dict[str, Any]:
        """Return ``{"scores": {...}, "reasoning": {...}}`` for one query."""
        ...


@dataclass(frozen=True, slots=True)
class QueryRow:
    """
    One typed per-query result row.

    Attributes:
        query_id: Dataset query identifier.
        question: The question text.
        gold_answer: The reference/gold answer.
        generated_answer: The RAG-generated answer text.
        relevant_passage_retrieved: Whether the gold passage was retrieved.
        faithfulness_score: Faithfulness metric score.
        context_precision_score: Contextual-precision metric score.
        context_recall_score: Contextual-recall metric score.
        answer_relevancy_score: Answer-relevancy metric score.
        judge_verdict: PASS / NEEDS_REVIEW / ERROR.
        total_ms: End-to-end RAG timing in milliseconds.
        error: Error string (empty when the query succeeded).

    """

    query_id: str
    question: str
    gold_answer: str
    generated_answer: str
    relevant_passage_retrieved: bool = False
    faithfulness_score: float = 0.0
    context_precision_score: float = 0.0
    context_recall_score: float = 0.0
    answer_relevancy_score: float = 0.0
    judge_verdict: str = "ERROR"
    total_ms: float = 0.0
    error: str = ""


@dataclass(frozen=True, slots=True)
class RunSummary:
    """
    Aggregate summary for a golden-set run.

    Attributes:
        success_count: Number of queries that completed without error.
        error_count: Number of queries that raised.
        num_queries: Total queries processed.
        metric_means: Mean per metric over the successful rows.

    """

    success_count: int
    error_count: int
    num_queries: int
    metric_means: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunResult:
    """
    Pure result of a golden-set run (no file I/O).

    Attributes:
        rows: The typed per-query rows in dataset order.
        summary: The aggregate run summary.

    """

    rows: tuple[QueryRow, ...]
    summary: RunSummary


def _metric_means(rows: tuple[QueryRow, ...]) -> dict[str, float]:
    """Compute per-metric means over the successful rows (empty -> zeros)."""
    successful = [r for r in rows if r.error == ""]
    if not successful:
        return {}
    n = len(successful)
    return {
        "faithfulness": sum(r.faithfulness_score for r in successful) / n,
        "context_precision": sum(r.context_precision_score for r in successful) / n,
        "context_recall": sum(r.context_recall_score for r in successful) / n,
        "answer_relevancy": sum(r.answer_relevancy_score for r in successful) / n,
    }


@beartype
def run_golden_set(
    *,
    dataset: Iterable[tuple[str, str, str, str]],
    adapter: RagAdapter,
    evaluator: _Evaluator,
    config: dict[str, Any],
    claims: ClaimStore | None = None,
    corpus_dir: Path | None = None,
) -> RunResult:
    """
    Run the golden-set evaluation loop and collect a ``RunResult``.

    The function is PURE with respect to the filesystem: it never opens a file
    or builds a results- or data-directory path. Deps are injected by the CLI shell.

    Args:
        dataset: Iterable of ``(query_id, question, relevant_passage_id, gold)``.
        adapter: The injected RAG adapter (built in the shell).
        evaluator: The injected metrics evaluator (kernel ``DeepEvalEvaluator``).
        config: Loaded configuration dict (read-only here).
        claims: Optional two-phase ``ClaimStore`` idempotency seam (monorepo
            concern; no-op when ``None`` in crucible standalone).
        corpus_dir: Optional corpus directory passed through to the adapter. The
            shell resolves the path from config; the library carries no default.

    Returns:
        A ``RunResult`` with the per-query rows + the aggregate summary.

    """
    _ = config  # config is reserved for future per-run knobs; read-only here.
    _ = claims  # no-op idempotency seam in crucible standalone.
    rows: list[QueryRow] = []
    success_count = 0
    error_count = 0

    dataset_list = list(dataset)
    num_queries = len(dataset_list)

    for query_id, query_text, relevant_passage_id, gold_answer in dataset_list:
        try:
            output = adapter.query(query_text, corpus_dir or Path("."))

            retrieved_chunks = output.get("retrieved_chunks", [])
            timings = output.get("timings_ms", {})
            generated_answer = output.get("answer", {}).get("text", "")
            relevant_passage_retrieved = any(
                chunk.get("doc_id") == relevant_passage_id for chunk in retrieved_chunks
            )

            metric_result = evaluator.compute_metrics_with_reasoning(output, gold_answer)
            scores = metric_result["scores"]
            faithfulness = scores.get("faithfulness", 0.0)
            verdict = "PASS" if faithfulness > _FAITHFULNESS_PASS_THRESHOLD else "NEEDS_REVIEW"

            rows.append(
                QueryRow(
                    query_id=query_id,
                    question=query_text,
                    gold_answer=gold_answer,
                    generated_answer=generated_answer,
                    relevant_passage_retrieved=relevant_passage_retrieved,
                    faithfulness_score=faithfulness,
                    context_precision_score=scores.get("context_precision", 0.0),
                    context_recall_score=scores.get("context_recall", 0.0),
                    answer_relevancy_score=scores.get("answer_relevancy", 0.0),
                    judge_verdict=verdict,
                    total_ms=timings.get("total", 0),
                    error="",
                )
            )
            success_count += 1
        except Exception as exc:  # noqa: BLE001 -- collect, never abort the run
            rows.append(
                QueryRow(
                    query_id=query_id,
                    question=query_text,
                    gold_answer=gold_answer,
                    generated_answer="",
                    judge_verdict="ERROR",
                    error=str(exc),
                )
            )
            error_count += 1

    rows_tuple = tuple(rows)
    summary = RunSummary(
        success_count=success_count,
        error_count=error_count,
        num_queries=num_queries,
        metric_means=_metric_means(rows_tuple),
    )
    return RunResult(rows=rows_tuple, summary=summary)


@beartype
def run_phoenix_native(
    *,
    rag_adapter: RagAdapter,
    corpus_dir: Path,
    endpoint: str,
    slice_name: str,
    experiment_name: str,
    judge_model: str,
) -> Any:
    """
    Run the Phoenix-native experiment path (different deps + return shape).

    Delegates to ``crucible.service.phoenix.experiments.run_phoenix_experiment``.
    The CLI shell exports the returned experiment object via
    ``export_experiment_results``.

    Args:
        rag_adapter: The injected RAG adapter.
        corpus_dir: Corpus directory (shell-resolved from config).
        endpoint: Phoenix endpoint URL.
        slice_name: Dataset slice name.
        experiment_name: Phoenix experiment name.
        judge_model: Resolved judge model id.

    Returns:
        The Phoenix ``RanExperiment`` object (or dict) for the shell to export.

    """
    from crucible.service.phoenix.experiments import run_phoenix_experiment

    return run_phoenix_experiment(
        rag_adapter=rag_adapter,
        corpus_dir=corpus_dir,
        judge_model=judge_model,
        endpoint=endpoint,
        slice_name=slice_name,
        experiment_name=experiment_name,
    )
