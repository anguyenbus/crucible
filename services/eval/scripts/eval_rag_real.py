"""Real RAG eval driven through the MIGRATED app (skeleton) -> local Phoenix.

Unlike smoke_phoenix.py (which fabricates context from the gold answer), this
injects a REAL retrieval+generation RAG -- the crucible ChromaDB stub, reusing the
already-ingested gst collection -- as the eval service's query callable, and runs
it through the migrated `app.runners.golden_set`. It proves the migrated service
evaluates a genuine RAG (real retrieval + OpenAI generation + the 4 OpenAI-judge
metrics) end-to-end and traces to a local Phoenix.

The injected RAG (`crucible.local.stubs`) does NOT migrate; it is wired in here
exactly as a CLI shell would in crucible -- the eval service (`app`) never imports
`local`/`crucible`. This driver is the shell.

Run from the CRUCIBLE REPO ROOT (so the stub's CWD-relative data/chromadb resolves
to the ingested collection) with the service venv + crucible/src on PYTHONPATH:

    set -a; . .env; set +a
    GST_CACHE_DIR=data/rag/gst_legal_rag GST_CORPUS_DIR=data/rag/gst_legal_rag SLICE=gst_pico \\
    PYTHONPATH=$PWD/src services/eval/.venv/bin/python services/eval/scripts/eval_rag_real.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.datasets.gst_legal_rag import load_gst_legal_rag
from app.deepeval.bedrock_provider import create_deepeval_metrics, get_deepeval_config
from app.kernel.interfaces import RagAdapter
from app.kernel.rag_metrics.evaluator import DeepEvalEvaluator
from app.phoenix.adapter import PhoenixAdapter, suppress_tracing_if_available
from app.runners.golden_set import run_golden_set

# The injected RAG: crucible's ChromaDB stub (retrieval + generation), reused as-is.
from crucible.local.stubs.rag.chromadb_query import query as stub_query

PHOENIX_PROJECT = "migrated-eval-rag-real"


def main() -> None:
    """Run a real-retrieval golden-set eval through the migrated app -> Phoenix."""
    cache_dir = Path(os.environ["GST_CACHE_DIR"])
    corpus_dir = Path(os.environ["GST_CORPUS_DIR"])  # .stem must match the collection name
    slice_name = os.environ.get("SLICE", "gst_pico")
    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    top_k = int(os.environ.get("TOP_K", "5"))

    def query_callable(question: str, corpus: Path, embedder: Any = None) -> dict[str, Any]:
        # Real retrieval + generation; reuses the persisted collection (no re-ingest).
        return stub_query(question, corpus, top_k=top_k, force_reingest=False)

    dataset = list(load_gst_legal_rag(cache_dir=cache_dir, slice=slice_name))
    print(f"[eval-rag] slice={slice_name} questions={len(dataset)} top_k={top_k}")

    adapter = RagAdapter(query_callable=query_callable)

    dc = get_deepeval_config({})
    print(f"[eval-rag] judge={dc['judge_model_provider']}/{dc['judge_model']} "
          f"generator={os.environ.get('CRUCIBLE_GENERATOR_MODEL')}")
    evaluator = DeepEvalEvaluator(
        metrics=create_deepeval_metrics(
            llm_provider=dc["judge_model_provider"],
            judge_model=dc["judge_model"],
            temperature=dc["temperature"],
        ),
        suppress_tracing=suppress_tracing_if_available,
    )

    phoenix = PhoenixAdapter(endpoint=endpoint, project_name=PHOENIX_PROJECT, enabled=True)
    print(f"[eval-rag] phoenix project={PHOENIX_PROJECT} connected={phoenix.is_connected()}")

    result = run_golden_set(
        dataset=dataset,
        adapter=adapter,
        evaluator=evaluator,
        config={},
        phoenix=phoenix,
        corpus_dir=corpus_dir,
    )

    print(f"\n[eval-rag] RunResult: success={result.summary.success_count} "
          f"errors={result.summary.error_count}")
    for row in result.rows:
        print(
            f"  q={row.query_id} verdict={row.judge_verdict} "
            f"faith={row.faithfulness_score} ctx_prec={row.context_precision_score:.3f} "
            f"ctx_rec={row.context_recall_score} ans_rel={row.answer_relevancy_score} "
            f"err={row.error[:60]!r}"
        )
    print(f"\n[eval-rag] view traces at {endpoint} -> Projects -> {PHOENIX_PROJECT}")


if __name__ == "__main__":
    main()
