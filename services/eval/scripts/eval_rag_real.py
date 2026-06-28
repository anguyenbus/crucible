"""Real RAG eval driven through the MIGRATED app -> Phoenix Datasets & Experiments.

Unlike smoke_phoenix.py (which fabricates context from the gold answer), this
injects a REAL retrieval+generation RAG -- the crucible ChromaDB stub, reusing the
already-ingested gst collection -- as the eval service's query callable, and runs
it through the migrated `app.runners.golden_set.run_phoenix_native`. It proves the
migrated service evaluates a genuine RAG (real retrieval + generation + the four
DeepEval LLM-judge metrics) end-to-end and lands a per-question, scored experiment
under Phoenix's Datasets & Experiments tab.

Flow B (Datasets & Experiments) is now the only Phoenix path: the legacy
span-tracing path has been retired. The judge runs on AWS Bedrock,
resolved via env (CRUCIBLE_JUDGE_PROVIDER=bedrock, CRUCIBLE_JUDGE_MODEL=<bedrock
inference-profile id>).

The injected RAG (`crucible.local.stubs`) does NOT migrate; it is wired in here
exactly as a CLI shell would in crucible -- the eval service (`app`) never imports
`local`/`crucible`. This driver is the shell.

Run from the CRUCIBLE REPO ROOT (so the stub's CWD-relative data/chromadb resolves
to the ingested collection) with the service venv + crucible/src on PYTHONPATH:

    set -a; . .env; set +a
    GST_CORPUS_DIR=data/rag/gst_legal_rag SLICE=gst_pico \\
    PYTHONPATH=$PWD/src services/eval/.venv/bin/python services/eval/scripts/eval_rag_real.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.deepeval.bedrock_provider import get_deepeval_config
from app.kernel.interfaces import RagAdapter
from app.runners.golden_set import run_phoenix_native

# The injected RAG: crucible's ChromaDB stub (retrieval + generation), reused as-is.
from crucible.local.stubs.rag.chromadb_query import query as stub_query

PHOENIX_PROJECT = "migrated-eval-rag-real"


def main() -> None:
    """Run a real-retrieval Phoenix-native experiment through the migrated app."""
    # corpus_dir serves BOTH the dataset upload (load questions.jsonl from here)
    # AND the injected RAG (collection name = corpus_dir.stem).
    corpus_dir = Path(os.environ.get("GST_CORPUS_DIR", "data/rag/gst_legal_rag"))
    slice_name = os.environ.get("SLICE", "gst_pico")
    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    top_k = int(os.environ.get("TOP_K", "5"))
    experiment_name = os.environ.get("EXPERIMENT_NAME", f"{PHOENIX_PROJECT}-{slice_name}")

    def query_callable(question: str, corpus: Path, embedder: Any = None) -> dict[str, Any]:
        # Real retrieval + generation; reuses the persisted collection (no re-ingest).
        return stub_query(question, corpus, top_k=top_k, force_reingest=False)

    adapter = RagAdapter(query_callable=query_callable)

    judge_model = get_deepeval_config({})["judge_model"]
    print(f"[eval-rag] slice={slice_name} judge={judge_model} top_k={top_k} "
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

    print(f"\n[eval-rag] experiment complete: "
          f"name={_attr(experiment, 'experiment_name')} id={_attr(experiment, 'experiment_id')} "
          f"dataset_id={_attr(experiment, 'dataset_id')}")
    print(f"[eval-rag] view it at {endpoint}/datasets -> the experiment row")


if __name__ == "__main__":
    main()
