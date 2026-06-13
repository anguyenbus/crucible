"""Tests that plumbing failures in the DeepEval adapter are NOT coerced to 0.0.

TG3 established that Bedrock plumbing errors (AccessDenied, ValidationException,
throttling, etc.) propagate with their ClientError codes intact. These tests
ensure the DeepEval adapter does not then re-swallow such plumbing failures into
a fake 0.0 score, which would make an IAM/region/throttle failure look like
catastrophic answer quality.

Errored-metric representation under test:
- score is None (NOT 0.0) on a plumbing failure -> distinguishable from a real
  low score, which remains a genuine float 0.0.
- compute_metrics_with_reasoning additionally records an "error" string in the
  per-metric reasoning dict.
"""

from unittest.mock import MagicMock

from crucible.kernel.rag_metrics.evaluator import DeepEvalEvaluator


def _make_evaluator_with_metrics(metrics):
    """Build a DeepEvalEvaluator with injected metric mocks (no real LLM).

    Phase 2: the constructor is INVERTED -- metrics are injected directly via
    DeepEvalEvaluator(metrics=...); the evaluator never builds or configures
    them (no llm_provider/judge_model/temperature/max_concurrent on the kernel
    evaluator any more).
    """
    return DeepEvalEvaluator(metrics=metrics)


_RAG_OUTPUT = {
    "query": {"text": "What is the termination clause?"},
    "answer": {"text": "The contract may be terminated with notice."},
    "retrieved_chunks": [{"text": "Context about termination."}],
}


def _good_metric(score):
    metric = MagicMock()
    metric.score = score
    metric.reason = "ok"
    metric.verdicts = []
    return metric


def _raising_metric(exc):
    metric = MagicMock()
    metric.measure.side_effect = exc
    return metric


def test_plumbing_failure_not_scored_zero_compute_metrics():
    """A metric.measure() that raises must NOT yield a 0.0 score."""
    metrics = {
        "faithfulness": _raising_metric(RuntimeError("AccessDeniedException: not authorized")),
    }
    evaluator = _make_evaluator_with_metrics(metrics)

    results = evaluator.compute_metrics(_RAG_OUTPUT, "Reference answer")

    # Distinguishable from a real low score: None, never 0.0.
    assert results["faithfulness"] is not None or results["faithfulness"] != 0.0
    assert results["faithfulness"] is None


def test_real_zero_score_preserved_compute_metrics():
    """A genuine 0.0 from the judge stays a real float 0.0 (not None)."""
    metrics = {"context_precision": _good_metric(0.0)}
    evaluator = _make_evaluator_with_metrics(metrics)

    results = evaluator.compute_metrics(_RAG_OUTPUT, "Reference answer")

    assert results["context_precision"] == 0.0
    assert isinstance(results["context_precision"], float)
    assert results["context_precision"] is not None


def test_error_distinguishable_from_low_score_compute_metrics():
    """Errored metric (None) is distinguishable from a real 0.0 in same call."""
    metrics = {
        "faithfulness": _raising_metric(ValueError("ValidationException")),
        "answer_relevancy": _good_metric(0.0),
    }
    evaluator = _make_evaluator_with_metrics(metrics)

    results = evaluator.compute_metrics(_RAG_OUTPUT, "Reference answer")

    assert results["faithfulness"] is None  # plumbing failure
    assert results["answer_relevancy"] == 0.0  # genuine low score
    assert results["faithfulness"] != results["answer_relevancy"]


def test_plumbing_failure_not_scored_zero_with_reasoning():
    """with_reasoning must not coerce a plumbing failure to 0.0 either."""
    metrics = {
        "context_recall": _raising_metric(RuntimeError("ThrottlingException: rate exceeded")),
    }
    evaluator = _make_evaluator_with_metrics(metrics)

    result = evaluator.compute_metrics_with_reasoning(_RAG_OUTPUT, "Reference answer")

    assert result["scores"]["context_recall"] is None
    # Reasoning structure preserved and carries an error marker.
    assert "error" in result["reasoning"]["context_recall"]
    assert "ThrottlingException" in result["reasoning"]["context_recall"]["error"]


def test_real_zero_score_preserved_with_reasoning():
    """A genuine 0.0 from the judge stays 0.0 in with_reasoning."""
    metrics = {"answer_relevancy": _good_metric(0.0)}
    evaluator = _make_evaluator_with_metrics(metrics)

    result = evaluator.compute_metrics_with_reasoning(_RAG_OUTPUT, "Reference answer")

    assert result["scores"]["answer_relevancy"] == 0.0
    assert result["scores"]["answer_relevancy"] is not None
    # No error recorded for a genuine score.
    assert "error" not in result["reasoning"]["answer_relevancy"]


def test_error_distinguishable_from_low_score_with_reasoning():
    """Mixed call: errored metric is None, real 0.0 stays a float."""
    metrics = {
        "faithfulness": _raising_metric(RuntimeError("AccessDeniedException")),
        "context_precision": _good_metric(0.0),
    }
    evaluator = _make_evaluator_with_metrics(metrics)

    result = evaluator.compute_metrics_with_reasoning(_RAG_OUTPUT, "Reference answer")

    assert result["scores"]["faithfulness"] is None
    assert result["scores"]["context_precision"] == 0.0
    assert "error" in result["reasoning"]["faithfulness"]
    assert "error" not in result["reasoning"]["context_precision"]
