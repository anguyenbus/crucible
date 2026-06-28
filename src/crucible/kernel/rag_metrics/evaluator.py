"""
DeepEval metrics evaluator (pure kernel, inverted constructor).

``DeepEvalEvaluator`` computes LLM-judge metrics over RAG output. Phase 2
INVERTED the constructor: it now receives an already-constructed metrics dict
(``DeepEvalEvaluator(metrics=...)``) and NEVER imports the service config. The
service builds the metrics (via
``crucible.service.deepeval.bedrock_provider.create_deepeval_metrics``) and
injects them, keeping the kernel pure (no boto3/config reach-in).

Tracing suppression is also INJECTED: the kernel must not import ``phoenix``, so
the phoenix-aware ``suppress_tracing`` factory is passed in by the impure
service/observability layer. When omitted the evaluator uses the pure
``noop_suppression`` default (behaviourally identical to "Phoenix not installed").
"""

from __future__ import annotations

import sys
import time

from beartype import beartype
from beartype.typing import Any, Callable, ContextManager, Dict, List

from crucible.kernel.rag_metrics.samples import (
    noop_suppression,
    transform_to_deepeval_sample,
)

# A factory producing a context manager that suppresses (or no-ops) tracing for
# the duration of a single metric.measure() call.
SuppressTracing = Callable[[], ContextManager[Any]]


@beartype
class DeepEvalEvaluator:
    """
    DeepEval metrics evaluator for RAG output.

    The DeepEvalEvaluator computes LLM-judge metrics including:
    - Faithfulness: Detects hallucinations in generated answers
    - ContextualPrecision: Measures signal-to-noise in retrieved contexts
    - ContextualRecall: Evaluates coverage of relevant information
    - AnswerRelevancy: Assesses directness of response to question

    The constructor is INVERTED: callers build the metric objects (e.g. via the
    service ``create_deepeval_metrics(...)``) and inject the resulting dict. The
    evaluator never constructs metrics itself and never imports config, so the
    kernel stays infra-free.

    Tracing suppression is injected via ``suppress_tracing`` (a factory returning
    a context manager). The phoenix-aware factory is built by the impure
    service/observability layer; the kernel default is a pure no-op so judge
    calls do not require Phoenix.

    Attributes:
        _metrics: Dictionary of DeepEval metric instances (injected).
        _suppress: Injected tracing-suppression factory (defaults to no-op).

    Example:
        >>> from crucible.service.deepeval.bedrock_provider import create_deepeval_metrics
        >>> metrics = create_deepeval_metrics(llm_provider="bedrock", judge_model=...)
        >>> evaluator = DeepEvalEvaluator(metrics=metrics)
        >>> scores = evaluator.compute_metrics(rag_output, reference_answer)
        >>> print(scores["faithfulness"])

    """

    __slots__ = ("_metrics", "_suppress")

    def __init__(
        self,
        metrics: Dict[str, Any],
        suppress_tracing: SuppressTracing | None = None,
    ) -> None:
        """
        Initialize the evaluator with already-constructed DeepEval metrics.

        Args:
            metrics: Mapping of metric name -> instantiated DeepEval metric
                object. Built by the caller (service layer) and injected; the
                kernel evaluator does not construct or configure metrics.
            suppress_tracing: Optional factory returning a context manager used to
                suppress noisy judge child-spans (e.g. a Phoenix
                ``suppress_tracing``). Injected by the impure service layer to
                keep the kernel free of any Phoenix import. Defaults to a pure
                no-op.

        """
        self._metrics = metrics
        self._suppress: SuppressTracing = suppress_tracing or noop_suppression

    @beartype
    def compute_metrics(
        self,
        rag_output: Dict[str, Any],
        reference_answer: str,
    ) -> Dict[str, Any]:
        """
        Compute DeepEval metrics for RAG output.

        Transforms the RAG output to DeepEval format and evaluates with
        all configured metrics (Faithfulness, ContextualPrecision,
        ContextualRecall, AnswerRelevancy).

        Args:
            rag_output: Dictionary conforming to legal_rag_bench schema.
            reference_answer: Reference answer text from the dataset.

        Returns:
            Dictionary mapping metric names to scores:
                - faithfulness: float (0-1)
                - context_precision: float (0-1)
                - context_recall: float (0-1)
                - answer_relevancy: float (0-1)

        Example:
            >>> scores = evaluator.compute_metrics(rag_output, "Reference answer")
            >>> print(f"Faithfulness: {scores['faithfulness']}")

        """
        # Transform to DeepEval format
        test_case = transform_to_deepeval_sample(rag_output, reference_answer)

        # Compute metrics
        results: Dict[str, Any] = {}

        # Compute Faithfulness
        if "faithfulness" in self._metrics:
            try:
                metric = self._metrics["faithfulness"]
                with self._suppress():
                    metric.measure(test_case)
                results["faithfulness"] = float(metric.score)
            except Exception as e:
                print(f"[ERROR] faithfulness failed: {e}", file=sys.stderr)
                # Plumbing failure (creds/region/throttle): record None,
                # NOT 0.0. None is distinguishable from a real low score.
                results["faithfulness"] = None

        # Compute ContextualPrecision
        if "context_precision" in self._metrics:
            try:
                metric = self._metrics["context_precision"]
                with self._suppress():
                    metric.measure(test_case)
                results["context_precision"] = float(metric.score)
            except Exception as e:
                print(f"[ERROR] context_precision failed: {e}", file=sys.stderr)
                # Plumbing failure (creds/region/throttle): record None,
                # NOT 0.0. None is distinguishable from a real low score.
                results["context_precision"] = None

        # Compute ContextualRecall
        if "context_recall" in self._metrics:
            try:
                metric = self._metrics["context_recall"]
                with self._suppress():
                    metric.measure(test_case)
                results["context_recall"] = float(metric.score)
            except Exception as e:
                print(f"[ERROR] context_recall failed: {e}", file=sys.stderr)
                # Plumbing failure (creds/region/throttle): record None,
                # NOT 0.0. None is distinguishable from a real low score.
                results["context_recall"] = None

        # Compute AnswerRelevancy
        if "answer_relevancy" in self._metrics:
            try:
                metric = self._metrics["answer_relevancy"]
                with self._suppress():
                    metric.measure(test_case)
                results["answer_relevancy"] = float(metric.score)
            except Exception as e:
                print(f"[ERROR] answer_relevancy failed: {e}", file=sys.stderr)
                # Plumbing failure (creds/region/throttle): record None,
                # NOT 0.0. None is distinguishable from a real low score.
                results["answer_relevancy"] = None

        return results

    @beartype
    def compute_metrics_with_timing(
        self,
        rag_output: Dict[str, Any],
        reference_answer: str,
    ) -> Dict[str, Any]:
        """
        Compute DeepEval metrics with timing information.

        Same as compute_metrics but includes metric_computation_time_ms
        in results.

        Args:
            rag_output: Dictionary conforming to legal_rag_bench schema.
            reference_answer: Reference answer text from the dataset.

        Returns:
            Dictionary mapping metric names to scores plus timing metadata:
                - faithfulness: float (0-1)
                - context_precision: float (0-1)
                - context_recall: float (0-1)
                - answer_relevancy: float (0-1)
                - metric_computation_time_ms: float

        """
        start_time = time.time()
        results = self.compute_metrics(rag_output, reference_answer)
        end_time = time.time()

        results["metric_computation_time_ms"] = (end_time - start_time) * 1000
        return results

    @beartype
    def _extract_verdicts(self, metric: Any) -> List[Dict[str, Any]]:
        """
        Extract verdicts from a DeepEval metric.

        Verdicts contain per-chunk or per-claim judgments with rationale.

        Args:
            metric: DeepEval metric instance after measure() has been called.

        Returns:
            List of verdict dictionaries with verdict, reason, and optionally
            chunk_id.

        """
        verdicts = getattr(metric, "verdicts", None)
        if not verdicts:
            return []

        extracted = []
        for v in verdicts:
            try:
                if hasattr(v, "model_dump"):
                    verdict_dict = v.model_dump()
                elif isinstance(v, dict):
                    verdict_dict = v
                elif hasattr(v, "__dict__"):
                    verdict_dict = vars(v)
                else:
                    verdict_dict = {"verdict": str(v)}
                extracted.append(verdict_dict)
            except Exception:
                # Fallback to string representation on any error
                extracted.append({"verdict": str(v)})

        return extracted

    @beartype
    def compute_metrics_with_reasoning(
        self,
        rag_output: Dict[str, Any],
        reference_answer: str,
    ) -> Dict[str, Any]:
        """
        Compute DeepEval metrics with full reasoning extraction.

        Extracts three layers of reasoning:
        - L1: metric.reason (overall explanation)
        - L2: metric.verdicts (per-chunk yes/no with rationale)
        - L3: metric.claims/truths (detailed claim analysis for Faithfulness)

        Args:
            rag_output: Dictionary conforming to legal_rag_bench schema.
            reference_answer: Reference answer text from the dataset.

        Returns:
            Dictionary with:
                - scores: dict of metric name -> float score
                - reasoning: dict of metric name -> dict with:
                    - reason: str (L1 overall explanation)
                    - verdicts: list[dict] (L2 per-chunk judgments)
                    - claims/truths: list[dict] (L3 breakdown for faithfulness)

        """
        # Transform to DeepEval format
        test_case = transform_to_deepeval_sample(rag_output, reference_answer)

        scores: Dict[str, Any] = {}
        reasoning: Dict[str, Any] = {}

        # Compute Faithfulness with reasoning
        if "faithfulness" in self._metrics:
            try:
                metric = self._metrics["faithfulness"]
                with self._suppress():
                    metric.measure(test_case)
                scores["faithfulness"] = float(metric.score)

                # Extract reasoning
                metric_reasoning = {
                    "reason": getattr(metric, "reason", ""),
                }
                reasoning["faithfulness"] = metric_reasoning

            except Exception as e:
                print(f"[ERROR] faithfulness failed: {e}", file=sys.stderr)
                # Plumbing failure: score is None (NOT 0.0) so it is
                # distinguishable from a real low score; reasoning carries
                # an explicit error marker.
                scores["faithfulness"] = None
                reasoning["faithfulness"] = {
                    "reason": f"ERROR: {e}",
                    "verdicts": [],
                    "error": str(e),
                }

        # Compute ContextualPrecision with reasoning
        if "context_precision" in self._metrics:
            try:
                metric = self._metrics["context_precision"]
                with self._suppress():
                    metric.measure(test_case)
                scores["context_precision"] = float(metric.score)

                # Extract reasoning - verdicts show which chunks were relevant
                reasoning["context_precision"] = {
                    "reason": getattr(metric, "reason", ""),
                    "verdicts": self._extract_verdicts(metric),
                }

            except Exception as e:
                print(f"[ERROR] context_precision failed: {e}", file=sys.stderr)
                # Plumbing failure: None (NOT 0.0), error marker recorded.
                scores["context_precision"] = None
                reasoning["context_precision"] = {
                    "reason": f"ERROR: {e}",
                    "verdicts": [],
                    "error": str(e),
                }

        # Compute ContextualRecall with reasoning
        if "context_recall" in self._metrics:
            try:
                metric = self._metrics["context_recall"]
                with self._suppress():
                    metric.measure(test_case)
                scores["context_recall"] = float(metric.score)

                reasoning["context_recall"] = {
                    "reason": getattr(metric, "reason", ""),
                    "verdicts": self._extract_verdicts(metric),
                }

            except Exception as e:
                print(f"[ERROR] context_recall failed: {e}", file=sys.stderr)
                # Plumbing failure: None (NOT 0.0), error marker recorded.
                scores["context_recall"] = None
                reasoning["context_recall"] = {
                    "reason": f"ERROR: {e}",
                    "verdicts": [],
                    "error": str(e),
                }

        # Compute AnswerRelevancy with reasoning
        if "answer_relevancy" in self._metrics:
            try:
                metric = self._metrics["answer_relevancy"]
                with self._suppress():
                    metric.measure(test_case)
                scores["answer_relevancy"] = float(metric.score)

                reasoning["answer_relevancy"] = {
                    "reason": getattr(metric, "reason", ""),
                }

            except Exception as e:
                print(f"[ERROR] answer_relevancy failed: {e}", file=sys.stderr)
                # Plumbing failure: None (NOT 0.0), error marker recorded.
                scores["answer_relevancy"] = None
                reasoning["answer_relevancy"] = {
                    "reason": f"ERROR: {e}",
                    "error": str(e),
                }

        return {"scores": scores, "reasoning": reasoning}
