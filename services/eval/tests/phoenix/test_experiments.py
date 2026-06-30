"""Unit tests for the Flow B Phoenix-native experiment path (MOCKED).

Flow B (``app.phoenix.experiments`` + ``...evaluators``) is the path
``eval-rag`` standardizes on after Flow A (``PhoenixAdapter`` span tracing) is
retired. Before this file Flow B had ZERO tests; per governing principle P1 this
safety net lands FIRST and must be GREEN before any Flow A deletion.

Everything here is MOCKED: the Phoenix ``Client`` and ``run_experiment`` are
patched (no Phoenix server) and the Bedrock judge is stubbed (no AWS / Bedrock
call). The suite runs hermetically in CI.

Coverage:
  * ``run_phoenix_experiment`` -- dataset creation + ``run_experiment`` invoked
    with FOUR evaluators built from a single explicitly-built judge.
  * ``create_rag_task`` -- the task-callable shape Phoenix's ``run_experiment``
    invokes per example.
  * ``export_experiment_results`` -- CSV + parquet + JSON written with the
    ``*_score`` / ``*_label`` / ``*_verdicts`` and cost columns.
  * The four ``create_*_evaluator`` factories build against the PASSED-IN judge
    instance, not a string default.
  * P2 judge contract -- ``_build_judge`` forces ``provider="bedrock"`` and the
    underlying resolver rejects any non-bedrock provider; the judge is built ONCE
    and the SAME instance flows into all four evaluators / ``run_experiment``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# experiments.py / evaluators.py hard-reference phoenix client + evals symbols at
# import time (guarded), and the evaluator factories raise without the phoenix
# extra. Skip cleanly when the extra is absent, mirroring test_adapter.py.
pytest.importorskip("phoenix.client", reason="requires the `phoenix` extra")
pytest.importorskip("phoenix.evals", reason="requires the `phoenix` extra")

from app.phoenix import evaluators as evaluators_mod  # noqa: E402
from app.phoenix import experiments as experiments_mod  # noqa: E402
from app.phoenix.experiments import (  # noqa: E402
    _build_judge,
    create_rag_task,
    export_experiment_results,
    run_phoenix_experiment,
)


class _StubJudge:
    """A stand-in for the DeepEval ``AmazonBedrockModel`` judge instance.

    It is a distinct object identity so tests can assert the SAME instance is
    threaded through ``_build_judge`` -> the four evaluators -> ``run_experiment``.
    """

    def __init__(self, model: str = "stub-judge") -> None:
        self.model = model


@pytest.fixture
def stub_judge() -> _StubJudge:
    return _StubJudge()


# --------------------------------------------------------------------------- #
# P2 judge contract: _build_judge forces Bedrock; resolver rejects non-bedrock
# --------------------------------------------------------------------------- #


def test_build_judge_calls_get_deepeval_llm_with_bedrock(monkeypatch, stub_judge):
    """``_build_judge`` must resolve the judge via ``provider="bedrock"``."""
    calls: list[dict[str, Any]] = []

    def fake_get_deepeval_llm(*, provider: str, model: str) -> _StubJudge:
        calls.append({"provider": provider, "model": model})
        return stub_judge

    # _build_judge imports get_deepeval_llm lazily from the bedrock_provider module.
    monkeypatch.setattr(
        "app.deepeval.bedrock_provider.get_deepeval_llm",
        fake_get_deepeval_llm,
    )

    judge = _build_judge("au.anthropic.claude-sonnet-4-5-20250929-v1:0")

    assert judge is stub_judge
    assert len(calls) == 1
    assert calls[0]["provider"] == "bedrock"
    assert calls[0]["model"] == "au.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_get_deepeval_llm_rejects_non_bedrock_provider():
    """The judge resolver underlying ``_build_judge`` refuses non-bedrock providers.

    This is the P2 footgun guard: a non-bedrock provider must raise rather than
    silently route the judge to OpenAI's ``GPTModel``.
    """
    from app.deepeval.bedrock_provider import get_deepeval_llm

    with pytest.raises(ValueError, match="bedrock"):
        get_deepeval_llm(provider="openai", model="gpt-4o")


# --------------------------------------------------------------------------- #
# run_phoenix_experiment: dataset creation + 4 evaluators from a single judge
# --------------------------------------------------------------------------- #


def test_run_phoenix_experiment_builds_four_evaluators_from_one_judge(monkeypatch, stub_judge):
    """One ``_build_judge`` call; the same judge instance flows into all four
    evaluators and into ``run_experiment`` with exactly four evaluators."""
    build_judge_calls: list[str] = []

    def fake_build_judge(judge_model: str) -> _StubJudge:
        build_judge_calls.append(judge_model)
        return stub_judge

    # Record the judge each factory was built with.
    factory_judges: dict[str, Any] = {}

    def make_fake_factory(metric_name: str):
        def _factory(*, judge_model: Any) -> str:
            factory_judges[metric_name] = judge_model
            return f"{metric_name}-evaluator"

        return _factory

    monkeypatch.setattr(experiments_mod, "_build_judge", fake_build_judge)
    monkeypatch.setattr(
        experiments_mod, "create_faithfulness_evaluator", make_fake_factory("faithfulness")
    )
    monkeypatch.setattr(
        experiments_mod,
        "create_context_precision_evaluator",
        make_fake_factory("context_precision"),
    )
    monkeypatch.setattr(
        experiments_mod, "create_context_recall_evaluator", make_fake_factory("context_recall")
    )
    monkeypatch.setattr(
        experiments_mod, "create_answer_relevancy_evaluator", make_fake_factory("answer_relevancy")
    )

    # Stub dataset creation + client/run_experiment so no Phoenix server is touched.
    fake_dataset = object()
    monkeypatch.setattr(
        experiments_mod, "create_phoenix_dataset", lambda **kwargs: fake_dataset
    )

    run_experiment_calls: list[dict[str, Any]] = []

    class _FakeExperiments:
        def run_experiment(self, **kwargs: Any) -> dict[str, Any]:
            run_experiment_calls.append(kwargs)
            return {"experiment_id": "exp-1"}

    class _FakeClient:
        def __init__(self) -> None:
            self.experiments = _FakeExperiments()

    monkeypatch.setattr(experiments_mod, "create_phoenix_client", lambda endpoint: _FakeClient())

    rag_adapter = object()
    result = run_phoenix_experiment(
        rag_adapter=rag_adapter,
        corpus_dir=Path("."),
        slice_name="pico",
        judge_model="au.anthropic.claude-sonnet-4-5-20250929-v1:0",
    )

    # Judge built exactly once.
    assert build_judge_calls == ["au.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    # Same judge instance into every factory.
    assert set(factory_judges) == {
        "faithfulness",
        "context_precision",
        "context_recall",
        "answer_relevancy",
    }
    for metric, judge in factory_judges.items():
        assert judge is stub_judge, f"{metric} did not receive the built judge instance"

    # run_experiment invoked once with the four evaluators + the created dataset.
    assert len(run_experiment_calls) == 1
    call = run_experiment_calls[0]
    assert call["dataset"] is fake_dataset
    assert len(call["evaluators"]) == 4
    assert result == {"experiment_id": "exp-1"}


def test_run_phoenix_experiment_requires_judge_model():
    """``judge_model`` is a REQUIRED parameter (no string default in Flow B)."""
    import inspect

    sig = inspect.signature(run_phoenix_experiment)
    judge_param = sig.parameters["judge_model"]
    assert judge_param.default is inspect.Parameter.empty, (
        "judge_model must be required (no string default) to avoid the DeepEval "
        "OpenAI-routing footgun"
    )


# --------------------------------------------------------------------------- #
# create_rag_task: task-callable shape
# --------------------------------------------------------------------------- #


def test_create_rag_task_returns_answer_and_retrieval_context():
    """The task callable maps a Phoenix example -> {answer, retrieval_context}."""

    class _FakeRag:
        def query(self, question: str, corpus_dir: Path) -> dict[str, Any]:
            assert question == "what is X?"
            return {
                "answer": {"text": "X is a thing."},
                "retrieved_chunks": [{"text": "chunk-1"}, {"text": "chunk-2"}],
            }

    task = create_rag_task(_FakeRag(), Path("."))

    out = task({"input": "what is X?"})
    assert out == {
        "answer": "X is a thing.",
        "retrieval_context": ["chunk-1", "chunk-2"],
    }

    # Accepts a bare string example too.
    out2 = task("what is X?")
    assert out2["answer"] == "X is a thing."


# --------------------------------------------------------------------------- #
# The four create_*_evaluator factories build against the PASSED-IN judge
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "factory_name",
    [
        "create_faithfulness_evaluator",
        "create_context_precision_evaluator",
        "create_context_recall_evaluator",
        "create_answer_relevancy_evaluator",
    ],
)
def test_evaluator_factories_require_judge_model(factory_name):
    """Each factory requires ``judge_model`` (no string default) -- P2 guard."""
    import inspect

    factory = getattr(evaluators_mod, factory_name)
    judge_param = inspect.signature(factory).parameters["judge_model"]
    assert judge_param.default is inspect.Parameter.empty, (
        f"{factory_name}: judge_model must be required (no string default)"
    )


@pytest.mark.parametrize(
    ("factory_name", "metric_attr"),
    [
        ("create_faithfulness_evaluator", "FaithfulnessMetric"),
        ("create_context_precision_evaluator", "ContextualPrecisionMetric"),
        ("create_context_recall_evaluator", "ContextualRecallMetric"),
        ("create_answer_relevancy_evaluator", "AnswerRelevancyMetric"),
    ],
)
def test_evaluator_builds_metric_with_passed_judge(
    monkeypatch, factory_name, metric_attr, stub_judge
):
    """The evaluator builds its DeepEval metric with the PASSED-IN judge instance.

    We patch the DeepEval metric class to capture the ``model=`` it is built with
    and confirm it is the stub judge object, not a string default.
    """
    import deepeval.metrics as dmetrics

    captured: dict[str, Any] = {}

    class _FakeMetric:
        def __init__(self, *, model: Any, include_reason: bool = True) -> None:
            captured["model"] = model
            self.threshold = 0.5
            self.success = True
            self.score = 1.0
            self.reason = "ok"
            self.evaluation_cost = 0.0
            self.verdicts = []

        def measure(self, test_case: Any) -> None:  # noqa: D401
            captured["measured"] = True

    monkeypatch.setattr(dmetrics, metric_attr, _FakeMetric)

    factory = getattr(evaluators_mod, factory_name)
    evaluator = factory(judge_model=stub_judge)

    # Invoke the underlying wrapped function (Phoenix's create_evaluator wraps it).
    inner = getattr(evaluator, "__wrapped__", None) or evaluator
    output = {"answer": "an answer", "retrieval_context": ["ctx"]}
    inner(output=output, input="a question?", expected="the expected answer")

    assert captured.get("model") is stub_judge, (
        f"{factory_name} did not build its metric with the passed-in judge instance"
    )


# --------------------------------------------------------------------------- #
# export_experiment_results: CSV + parquet + JSON with the column contract
# --------------------------------------------------------------------------- #


def _fake_experiment() -> dict[str, Any]:
    """A minimal RanExperiment-shaped dict with one task run + four eval results."""
    return {
        "experiment_id": "exp-42",
        "dataset_id": "",  # empty -> skip the dataset fetch network path
        "experiment_name": "rag-eval-pico",
        "task_runs": [
            {
                "id": "run-1",
                "dataset_example_id": "ex-1",
                "output": {"answer": "the answer"},
                "error": None,
                "cost": 0.0012,
                "latency": 1500,
            }
        ],
        "evaluation_runs": [
            {
                "experiment_run_id": "run-1",
                "result": [
                    {
                        "name": "faithfulness",
                        "score": 0.9,
                        "label": "faithful",
                        "metadata": {
                            "evaluation_cost": 0.0001,
                            "verdicts": [{"verdict": "yes", "reason": "supported"}],
                        },
                    },
                    {
                        "name": "context_precision",
                        "score": 0.8,
                        "label": "precise",
                        "metadata": {
                            "evaluation_cost": 0.0001,
                            "verdicts": [{"verdict": "yes", "reason": "relevant"}],
                        },
                    },
                    {
                        "name": "context_recall",
                        "score": 0.7,
                        "label": "high_recall",
                        "metadata": {
                            "evaluation_cost": 0.0001,
                            "verdicts": [{"verdict": "yes", "reason": "covered"}],
                        },
                    },
                    {
                        "name": "answer_relevancy",
                        "score": 0.95,
                        "label": "relevant",
                        "metadata": {
                            "evaluation_cost": 0.0001,
                            "verdicts": [{"verdict": "yes", "reason": "on-topic"}],
                        },
                    },
                ],
            }
        ],
    }


def test_export_experiment_results_writes_three_artifacts(tmp_path):
    """CSV + parquet + JSON are all written and the returned paths exist."""
    paths = export_experiment_results(_fake_experiment(), tmp_path)

    assert Path(paths["csv_path"]).exists()
    assert Path(paths["parquet_path"]).exists()
    assert Path(paths["json_path"]).exists()
    assert paths["csv_path"].endswith("rag-eval-pico_results.csv")
    assert paths["parquet_path"].endswith("rag-eval-pico_results.parquet")
    assert paths["json_path"].endswith("rag-eval-pico_summary.json")


def test_export_experiment_results_column_contract(tmp_path):
    """CSV carries the ``*_score`` / ``*_label`` / ``*_verdicts`` + cost columns."""
    import pandas as pd

    paths = export_experiment_results(_fake_experiment(), tmp_path)
    df = pd.read_csv(paths["csv_path"])

    for metric in ("faithfulness", "context_precision", "context_recall", "answer_relevancy"):
        assert f"{metric}_score" in df.columns
        assert f"{metric}_label" in df.columns
        assert f"{metric}_verdicts" in df.columns

    for cost_col in ("app_cost_usd", "judge_cost_usd", "total_cost_usd"):
        assert cost_col in df.columns

    # The single row carries the scores/labels we fed in.
    row = df.iloc[0]
    assert row["faithfulness_score"] == 0.9
    assert row["faithfulness_label"] == "faithful"

    # Parquet round-trips the same column contract.
    pq = pd.read_parquet(paths["parquet_path"])
    for metric in ("faithfulness", "context_precision", "context_recall", "answer_relevancy"):
        assert f"{metric}_score" in pq.columns

    # JSON summary carries the metric averages.
    summary = json.loads(Path(paths["json_path"]).read_text())
    assert summary["experiment_id"] == "exp-42"
    assert "faithfulness_score" in summary["metrics_avg"]
