"""Phoenix-NATIVE RAG eval through the migrated app -> Datasets & Experiments.

run_golden_set (smoke_phoenix.py / eval_rag_real.py) emits span TRACES to Phoenix
Projects. To see a scored result under Phoenix's **Datasets & Experiments** tab you
need the phoenix-native path: it uploads the slice as a Phoenix *dataset* and runs a
Phoenix *experiment* with the four DeepEval metrics as evaluators.

This drives the migrated `app.runners.golden_set.run_phoenix_native` with a REAL
injected RAG (crucible's ChromaDB stub, reusing the persisted gst collection). The
injected RAG does not migrate; this driver is the shell (app never imports
local/crucible).

Run from the CRUCIBLE REPO ROOT (so the stub's CWD-relative data/chromadb resolves
to the ingested collection), service venv + crucible/src on PYTHONPATH:

    set -a; . .env; set +a
    GST_CORPUS_DIR=data/rag/gst_legal_rag SLICE=gst_pico \\
    PYTHONPATH=$PWD/src services/eval/.venv/bin/python \\
        services/eval/scripts/eval_rag_phoenix_native.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.deepeval.bedrock_provider import get_deepeval_config
from app.kernel.interfaces import RagAdapter
from app.runners.golden_set import run_phoenix_native

from crucible.local.stubs.rag.chromadb_query import query as stub_query


def main() -> None:
    """Upload the slice + run a Phoenix experiment over a real RAG via the migrated app."""
    # corpus_dir serves BOTH the dataset upload (load questions.jsonl from here) AND the
    # injected RAG (collection name = corpus_dir.stem must be "gst_legal_rag").
    corpus_dir = Path(os.environ.get("GST_CORPUS_DIR", "data/rag/gst_legal_rag"))
    slice_name = os.environ.get("SLICE", "gst_pico")
    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    top_k = int(os.environ.get("TOP_K", "5"))
    experiment_name = os.environ.get("EXPERIMENT_NAME", f"gst-legal-rag-{slice_name}-migrated")

    def query_callable(question: str, corpus: Path, embedder: Any = None) -> dict[str, Any]:
        return stub_query(question, corpus, top_k=top_k, force_reingest=False)

    adapter = RagAdapter(query_callable=query_callable)
    judge_model = get_deepeval_config({})["judge_model"]
    print(f"[phoenix-native] slice={slice_name} judge={judge_model} "
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

    print(f"\n[phoenix-native] experiment complete: "
          f"name={_attr(experiment, 'experiment_name')} id={_attr(experiment, 'experiment_id')} "
          f"dataset_id={_attr(experiment, 'dataset_id')}")
    print(f"[phoenix-native] view it at {endpoint}/datasets "
          f"-> dataset '{experiment_name.replace('-migrated','')}' -> the experiment row")


if __name__ == "__main__":
    main()
