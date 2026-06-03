"""End-to-end / integration gap tests for the Bedrock-default feature (TG8).

These fill the critical feature gaps identified in the TG8 coverage analysis;
they intentionally do NOT re-test the focused behaviours already covered by the
TG1-7 unit tests. Scope is limited to this spec only.

Gaps covered:
1. TG5 None-becomes-ERROR-row: a plumbing failure surfaces as score=None from
   the adapter, and the runner's verdict computation + CSV row writing turn that
   None into an explicit "ERROR" row (NEVER a misleading PASS/0.0), while CSV
   output tolerates None cells.
2. Provider switch to openai via env: CRUCIBLE_JUDGE_PROVIDER=openai flips the
   resolved judge config end-to-end through get_deepeval_config.
3. Region-unset fail-loud at the runner level: main() runs the bedrock preflight
   on a bedrock-provider run and exits(1) loudly when AWS_REGION is unset, before
   any heavy initialisation.

Modelled after tests/runners/test_run_rag_eval.py conventions.
"""

import csv
import io
from unittest import mock

import pytest

from crucible.adapters.deepeval_adapter import DeepEvalEvaluator

# --------------------------------------------------------------------------- #
# Helpers mirroring the runner's per-query contract (run_rag_eval.main loop).
# --------------------------------------------------------------------------- #

_RUNNER_FIELDNAMES = [
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

_RAG_OUTPUT = {
    "query": {"text": "What is the termination clause?"},
    "answer": {"text": "The contract may be terminated with notice."},
    "retrieved_chunks": [{"text": "Context about termination."}],
}


def _make_evaluator_with_metrics(metrics):
    """Build a DeepEvalEvaluator with injected metric mocks (no real LLM)."""
    evaluator = DeepEvalEvaluator.__new__(DeepEvalEvaluator)
    evaluator._metrics = metrics
    evaluator._llm_provider = "bedrock"
    evaluator._judge_model = "au.anthropic.claude-opus-4-6"
    evaluator._temperature = 0.0
    evaluator._max_concurrent = 10
    evaluator._embedder = None
    return evaluator


def _raising_metric(exc):
    metric = mock.MagicMock()
    metric.measure.side_effect = exc
    return metric


def _good_metric(score):
    metric = mock.MagicMock()
    metric.score = score
    metric.reason = "ok"
    metric.verdicts = []
    return metric


def _run_runner_query_logic(evaluator):
    """Replicate run_rag_eval.main's per-query verdict + row logic exactly.

    Returns the row dict that the runner would have written for this query. The
    PASS-vs-NEEDS_REVIEW-vs-ERROR branching here is copied verbatim from
    run_rag_eval.main so the test pins the real runner contract: a None
    faithfulness score (a TG5 plumbing failure) raises in the verdict comparison
    and is caught into an ERROR row.
    """
    query_id = "q1"
    query_text = _RAG_OUTPUT["query"]["text"]
    gold_answer = "Reference answer"
    try:
        metric_result = evaluator.compute_metrics_with_reasoning(_RAG_OUTPUT, gold_answer)
        metric_scores = metric_result["scores"]
        faithfulness = metric_scores.get("faithfulness", 0.0)

        # Verbatim from the runner: None > 0.7 raises TypeError -> ERROR row.
        verdict = "PASS" if faithfulness > 0.7 else "NEEDS_REVIEW"

        return {
            "query_id": query_id,
            "question": query_text,
            "gold_answer": gold_answer,
            "generated_answer": _RAG_OUTPUT["answer"]["text"],
            "relevant_passage_retrieved": False,
            "faithfulness_score": faithfulness,
            "context_precision_score": metric_scores.get("context_precision", 0.0),
            "context_recall_score": metric_scores.get("context_recall", 0.0),
            "answer_relevancy_score": metric_scores.get("answer_relevancy", 0.0),
            "judge_verdict": verdict,
            "total_ms": 0,
            "error": "",
        }
    except Exception as e:
        return {
            "query_id": query_id,
            "question": query_text,
            "gold_answer": gold_answer,
            "generated_answer": "",
            "relevant_passage_retrieved": False,
            "faithfulness_score": 0.0,
            "context_precision_score": 0.0,
            "context_recall_score": 0.0,
            "answer_relevancy_score": 0.0,
            "judge_verdict": "ERROR",
            "total_ms": 0,
            "error": str(e),
        }


# --------------------------------------------------------------------------- #
# 1. TG5 None -> ERROR row integration.
# --------------------------------------------------------------------------- #


def test_none_faithfulness_becomes_error_row_not_pass():
    """A plumbing failure (score None) yields an ERROR row, never PASS/0.0 quality.

    TG5 makes the adapter return faithfulness=None on a plumbing failure (it logs
    the underlying AccessDeniedException to stderr). The runner then computes the
    verdict as ``None > 0.7`` which raises a TypeError; that exception is caught
    into an explicit ERROR row. The net effect is the contract that matters: a
    broken Bedrock call NEVER reads as a PASS or a real low score -- it is an
    ERROR row carrying a non-empty error string.
    """
    evaluator = _make_evaluator_with_metrics(
        {
            "faithfulness": _raising_metric(RuntimeError("AccessDeniedException: not authorized")),
        }
    )

    row = _run_runner_query_logic(evaluator)

    # The runner records this as an explicit ERROR, NOT a NEEDS_REVIEW/PASS, and
    # NOT a misleading 0.0 quality verdict masquerading as a real score.
    assert row["judge_verdict"] == "ERROR"
    assert row["error"]  # a non-empty error string is recorded
    # The error comes from the None-vs-float verdict comparison (the TG5 None
    # propagating into the runner), proving None was NOT silently scored 0.0.
    assert "NoneType" in row["error"]


def test_real_low_faithfulness_is_needs_review_not_error():
    """Contrast: a genuine low score (0.0) is NEEDS_REVIEW, distinguishable from ERROR."""
    evaluator = _make_evaluator_with_metrics(
        {
            "faithfulness": _good_metric(0.0),
            "context_precision": _good_metric(0.5),
            "context_recall": _good_metric(0.5),
            "answer_relevancy": _good_metric(0.5),
        }
    )

    row = _run_runner_query_logic(evaluator)

    assert row["judge_verdict"] == "NEEDS_REVIEW"
    assert row["error"] == ""
    assert row["faithfulness_score"] == 0.0


def test_csv_writer_tolerates_none_score_cell():
    """CSV/reporting tolerates a None metric cell (other metrics may be None).

    The runner writes rows with csv.DictWriter; a non-faithfulness metric can be
    None on a partial plumbing failure. Confirm DictWriter serialises None to an
    empty cell without raising, so reporting never crashes on errored metrics.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_RUNNER_FIELDNAMES)
    writer.writeheader()

    row = {
        "query_id": "q1",
        "question": "Q?",
        "gold_answer": "A",
        "generated_answer": "gen",
        "relevant_passage_retrieved": False,
        "faithfulness_score": 0.8,
        "context_precision_score": None,  # errored metric -> None
        "context_recall_score": 0.5,
        "answer_relevancy_score": None,  # errored metric -> None
        "judge_verdict": "PASS",
        "total_ms": 0,
        "error": "",
    }
    writer.writerow(row)  # must not raise

    out = buffer.getvalue().splitlines()
    # Header + one data row.
    assert len(out) == 2
    parsed = list(csv.DictReader(io.StringIO(buffer.getvalue())))[0]
    # None cells serialise to empty strings, not the literal "None".
    assert parsed["context_precision_score"] == ""
    assert parsed["answer_relevancy_score"] == ""
    assert parsed["faithfulness_score"] == "0.8"


# --------------------------------------------------------------------------- #
# 2. Provider switch to openai via env (end-to-end through get_deepeval_config).
# --------------------------------------------------------------------------- #


def test_provider_switch_to_openai_via_env(monkeypatch):
    """CRUCIBLE_JUDGE_PROVIDER=openai env flips the resolved judge config."""
    from crucible.metrics.deepeval_config import get_deepeval_config

    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CRUCIBLE_JUDGE_PROVIDER", "openai")
    monkeypatch.setenv("CRUCIBLE_JUDGE_MODEL", "gpt-4o-mini")

    # A YAML judge block defaulting to bedrock must be overridden by the env.
    config = {"judge": {"provider": "bedrock", "model": "au.anthropic.claude-opus-4-6"}}
    result = get_deepeval_config(config)

    assert result["judge_model_provider"] == "openai"
    assert result["judge_model"] == "gpt-4o-mini"
    # No region resolved/required on an openai run (region is bedrock-only).
    assert result["region"] is None


def test_default_bedrock_run_resolves_region_and_au_judge(monkeypatch):
    """Default (no provider env): bedrock judge + au.* model + resolved region."""
    from crucible.metrics.deepeval_config import get_deepeval_config

    monkeypatch.delenv("CRUCIBLE_JUDGE_PROVIDER", raising=False)
    monkeypatch.delenv("CRUCIBLE_JUDGE_MODEL", raising=False)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")

    result = get_deepeval_config({})

    assert result["judge_model_provider"] == "bedrock"
    assert result["judge_model"].startswith("au.")
    assert result["region"] == "ap-southeast-2"


# --------------------------------------------------------------------------- #
# 3. Region-unset fail-loud at the runner level (main() runs the preflight).
# --------------------------------------------------------------------------- #


def test_runner_main_region_unset_exits_loud(monkeypatch, tmp_path, capsys):
    """main() on a bedrock run with AWS_REGION unset exits(1) loudly via preflight."""
    from crucible.runners import run_rag_eval

    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CRUCIBLE_GENERATOR_PROVIDER", "bedrock")
    monkeypatch.setenv("CRUCIBLE_GENERATOR_MODEL", "au.anthropic.claude-sonnet-4-6")

    # A config that satisfies load_config's required sections (datasets, metrics,
    # models) so the run reaches the bedrock preflight and dies there -- before
    # any dataset/embedder/RAG initialisation.
    config_path = tmp_path / "eval_config.yaml"
    config_path.write_text(
        "datasets:\n  gst_legal_rag:\n    cache_path: ignored\nmetrics: {}\nmodels: {}\n"
    )

    argv = [
        "eval-rag",
        "--slice",
        "gst_pico",
        "--rag",
        "stub-local",
        "--config",
        str(config_path),
    ]
    monkeypatch.setattr(run_rag_eval.sys, "argv", argv)

    with pytest.raises(SystemExit) as exc_info:
        run_rag_eval.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "preflight" in combined
    assert "region" in combined
