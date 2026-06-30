"""Live Flow B experiment test (``@phoenix_integration``, opt-in).

Skips cleanly when no Phoenix server is reachable (the conftest marker hook adds
the skip when ``PHOENIX_ENDPOINT`` is unreachable). This is the NICE-TO-HAVE live
counterpart to the mocked ``test_experiments.py`` net: it drives a tiny dataset
through ``run_phoenix_experiment`` against a real Phoenix and asserts a scored
experiment plus the canonical CSV/parquet/JSON artifacts.

Requires the ``phoenix`` extra AND AWS Bedrock creds (the judge is a real
``AmazonBedrockModel``). It is excluded from the default hermetic suite via the
marker + ``-m "not phoenix_integration"``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("phoenix.client", reason="requires the `phoenix` extra")
pytest.importorskip("phoenix.evals", reason="requires the `phoenix` extra")

pytestmark = pytest.mark.phoenix_integration


class _GoldContextRag:
    """A trivial RAG that answers from a fixed context (no retrieval index).

    Mirrors the smoke-style injected RAG so the live test exercises real judge
    scoring + the Phoenix experiment round-trip without any ChromaDB dependency.
    """

    def query(self, question: str, corpus_dir: Path) -> dict[str, Any]:
        context = "Eval evaluates RAG systems using DeepEval metrics on Bedrock."
        return {
            "answer": {"text": "Eval evaluates RAG systems with DeepEval on Bedrock."},
            "retrieved_chunks": [{"text": context, "doc_id": "ctx-1"}],
        }


def test_live_flow_b_experiment_writes_artifacts(tmp_path, monkeypatch):
    """A live Phoenix experiment scores a tiny dataset and exports artifacts."""
    from app.phoenix import experiments as experiments_mod
    from app.phoenix.experiments import (
        export_experiment_results,
        run_phoenix_experiment,
    )

    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")

    # Use a fixed one-row dataset so the live run is cheap. We bypass the slice
    # loaders by stubbing create_phoenix_dataset to upload a single example.
    def _create_one_row_dataset(*, client: Any, corpus_dir: Path, slice_name: str) -> Any:
        name = f"flow-b-live-test-{slice_name}"
        try:
            return client.datasets.get_dataset(dataset=name)
        except Exception:
            return client.datasets.create_dataset(
                name=name,
                inputs=[{"input": "What does Eval evaluate?"}],
                outputs=[{"expected": "RAG systems via DeepEval metrics on Bedrock."}],
                metadata=[{"query_id": "live-1", "relevant_passage_id": "ctx-1"}],
                input_keys=["input"],
                output_keys=["expected"],
                dataset_description="Flow B live test single-row dataset",
            )

    monkeypatch.setattr(experiments_mod, "create_phoenix_dataset", _create_one_row_dataset)

    judge_model = os.environ.get(
        "EVAL_JUDGE_MODEL", "au.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )

    experiment = run_phoenix_experiment(
        rag_adapter=_GoldContextRag(),
        corpus_dir=tmp_path,
        endpoint=endpoint,
        slice_name="livetest",
        experiment_name="flow-b-live-test",
        judge_model=judge_model,
    )

    assert experiment is not None

    paths = export_experiment_results(experiment, tmp_path)
    assert Path(paths["csv_path"]).exists()
    assert Path(paths["parquet_path"]).exists()
    assert Path(paths["json_path"]).exists()
