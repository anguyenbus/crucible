"""Tests for the inverted kernel DeepEvalEvaluator and sample transform.

Phase 2 (Commit 6) splits the DeepEval adapter into the kernel:
- ``app.kernel.rag_metrics.evaluator.DeepEvalEvaluator`` now receives an
  already-constructed metrics dict via ``DeepEvalEvaluator(metrics=...)`` and
  NEVER imports config (the old ``create_deepeval_metrics`` reach-in is gone).
- ``app.kernel.rag_metrics.samples.transform_to_deepeval_sample`` produces
  the DeepEval LLMTestCase shape.

Deterministic only (NO hypothesis).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from app.kernel.rag_metrics.evaluator import DeepEvalEvaluator
from app.kernel.rag_metrics.samples import transform_to_deepeval_sample

_RAG_OUTPUT = {
    "query": {"text": "What is the termination clause?"},
    "answer": {"text": "The contract may be terminated with notice."},
    "retrieved_chunks": [{"text": "Context about termination."}, {"text": "More context."}],
}


def _good_metric(score):
    metric = MagicMock()
    metric.score = score
    metric.reason = "ok"
    metric.verdicts = []
    return metric


def test_constructor_injects_metrics_without_importing_config() -> None:
    """DeepEvalEvaluator(metrics=...) stores the dict and does NOT import config."""
    # Drop any cached config module so we can prove construction does not load it.
    sys.modules.pop("app.deepeval.bedrock_provider", None)

    metrics = {"faithfulness": _good_metric(0.9)}
    evaluator = DeepEvalEvaluator(metrics=metrics)

    assert evaluator._metrics is metrics
    # The inverted constructor must not pull in the service config module.
    assert "app.deepeval.bedrock_provider" not in sys.modules


def test_compute_metrics_uses_injected_metrics() -> None:
    """compute_metrics measures the injected metric and returns its float score."""
    metrics = {"faithfulness": _good_metric(0.85)}
    evaluator = DeepEvalEvaluator(metrics=metrics)

    results = evaluator.compute_metrics(_RAG_OUTPUT, "Reference answer")

    assert results["faithfulness"] == 0.85
    metrics["faithfulness"].measure.assert_called_once()


def test_transform_to_deepeval_sample_shape() -> None:
    """transform_to_deepeval_sample maps the RAG output to the LLMTestCase shape."""
    sample = transform_to_deepeval_sample(_RAG_OUTPUT, "Reference answer")

    assert sample.input == "What is the termination clause?"
    assert sample.actual_output == "The contract may be terminated with notice."
    assert sample.expected_output == "Reference answer"
    assert sample.retrieval_context == ["Context about termination.", "More context."]
