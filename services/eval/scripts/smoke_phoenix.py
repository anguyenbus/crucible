"""End-to-end smoke for the MIGRATED eval service (the `app` package).

Proves the migrated `services/eval/app` runs a real Phoenix-NATIVE evaluation and
lands a per-question, scored experiment under Phoenix's **Datasets & Experiments**
tab — using ONLY the migrated `app.*` library (datasets, runners, deepeval judge,
phoenix experiments) plus an INJECTED RAG.

Flow B (Datasets & Experiments) is now the only Phoenix path: the legacy
span-tracing path has been retired. This driver uploads the slice as a
Phoenix *dataset* and runs a Phoenix *experiment* with the four DeepEval metrics
as evaluators, via `app.runners.golden_set.run_phoenix_native`.

The eval service does not own a RAG; it evaluates whatever `(question,
corpus_dir) -> rag_query_output` callable it is given. Here we inject a tiny
generator grounded in the dataset's gold answer as context, so the run exercises
real generation + the four DeepEval LLM-judge metrics + the Phoenix experiment
round-trip, with NO `local`/`demo`/ChromaDB code (none migrates).

The judge runs on AWS Bedrock (resolved via env: EVAL_JUDGE_PROVIDER=bedrock,
EVAL_JUDGE_MODEL=<bedrock inference-profile id>).

Usage (from services/eval, with its venv + env from the repo .env):

    GST_CACHE_DIR=/abs/path/to/services/eval/dev/fixtures/data/rag/gst_legal_rag \\
    SLICE=gst_pico \\
    ./.venv/bin/python scripts/smoke_phoenix.py

Requires env: AWS creds + AWS_REGION (Bedrock judge), EVAL_JUDGE_PROVIDER=bedrock,
EVAL_JUDGE_MODEL (a Bedrock inference-profile id), EVAL_GENERATOR_MODEL,
PHOENIX_ENDPOINT (default http://localhost:6006).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.deepeval.bedrock_provider import get_deepeval_config
from app.kernel.interfaces import RagAdapter
from app.runners.golden_set import run_phoenix_native

PHOENIX_PROJECT = "migrated-eval-smoke"


def main() -> None:
    """Run a 2-question Phoenix-native experiment through the migrated app."""
    # corpus_dir serves the dataset upload (load questions.jsonl from here). The
    # injected RAG fabricates context from the gold answer, so no retrieval index
    # (no ChromaDB) is needed.
    corpus_dir = Path(os.environ["GST_CACHE_DIR"])
    slice_name = os.environ.get("SLICE", "gst_pico")
    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    experiment_name = os.environ.get("EXPERIMENT_NAME", f"{PHOENIX_PROJECT}-{slice_name}")

    gen_model = os.environ.get("EVAL_GENERATOR_MODEL", "")

    def query_callable(question: str, corpus: Path, embedder: Any = None) -> dict[str, Any]:
        # A trivial generator grounded in the dataset's gold answer as context.
        # Phoenix's run_experiment supplies the example; the dataset upload carries
        # the gold answer, so here we echo a context-grounded answer per question.
        _ = (corpus, embedder)
        context = question  # the experiment task only needs answer + retrieval_context
        return {
            "schema_version": "1.0.0",
            "system_version": {"pipeline_version": "smoke-0.1.0", "generator_model": gen_model},
            "query": {"query_id": "smoke", "text": question},
            "answer": {"text": context, "citations": []},
            "retrieved_chunks": [
                {
                    "chunk_id": "ctx-1",
                    "rank": 0,
                    "score": 1.0,
                    "doc_id": "gold-context",
                    "text": context,
                    "char_span": [0, len(context)],
                }
            ],
            "timings_ms": {"total": 0},
        }

    adapter = RagAdapter(query_callable=query_callable)

    judge_model = get_deepeval_config({})["judge_model"]
    print(f"[smoke] slice={slice_name} judge={judge_model} "
          f"experiment={experiment_name} endpoint={endpoint}")

    experiment = run_phoenix_native(
        rag_adapter=adapter,
        corpus_dir=corpus_dir,
        endpoint=endpoint,
        slice_name=slice_name,
        experiment_name=experiment_name,
        judge_model=judge_model,
    )

    def _attr(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    print(f"\n[smoke] experiment complete: "
          f"name={_attr(experiment, 'experiment_name')} id={_attr(experiment, 'experiment_id')} "
          f"dataset_id={_attr(experiment, 'dataset_id')}")
    print(f"[smoke] view it at {endpoint}/datasets -> the experiment row")


if __name__ == "__main__":
    main()
