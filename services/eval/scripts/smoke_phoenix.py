"""End-to-end smoke for the MIGRATED eval service (the `app` package).

Proves the migrated `services/eval/app` runs a real golden-set evaluation and
emits traces to a local Phoenix — using ONLY the migrated `app.*` library
(datasets, runners, deepeval judge, phoenix adapter) plus an INJECTED RAG.

The eval service does not own a RAG; it evaluates whatever `(question,
corpus_dir) -> rag_query_output` callable it is given. Here we inject a tiny
OpenAI-backed generator grounded in the dataset's gold answer as context, so the
run exercises real LLM generation + the four DeepEval LLM-judge metrics + Phoenix
span export, with NO `local`/`demo`/ChromaDB code (none migrates).

Usage (from services/eval, with its venv + env from crucible/.env):

    GST_CACHE_DIR=/abs/path/to/crucible/data/rag/gst_legal_rag \\
    SLICE=gst_pico \\
    ./.venv/bin/python scripts/smoke_phoenix.py

Requires env: OPENAI_API_KEY, CRUCIBLE_JUDGE_PROVIDER=openai,
CRUCIBLE_JUDGE_MODEL=gpt-4o, CRUCIBLE_GENERATOR_MODEL=gpt-4o-mini (judge != gen),
PHOENIX_ENDPOINT (default http://localhost:6006).
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

PHOENIX_PROJECT = "migrated-eval-smoke"


def _build_query_callable(context_by_question: dict[str, str]) -> Any:
    """Inject an OpenAI-backed RAG: generate an answer grounded in gold context.

    No retrieval index (no ChromaDB) — the dataset's gold answer is the context,
    so the run exercises real generation + judging without any demo dependency.
    """
    from openai import OpenAI

    client = OpenAI()
    gen_model = os.environ.get("CRUCIBLE_GENERATOR_MODEL", "gpt-4o-mini")

    def query_callable(question: str, corpus_dir: Path, embedder: Any = None) -> dict[str, Any]:
        _ = (corpus_dir, embedder)  # unsupplied by this injected RAG
        context = context_by_question.get(question, "")
        resp = client.chat.completions.create(
            model=gen_model,
            temperature=0.0,
            messages=[
                {
                    "role": "system",
                    "content": "Answer the question using ONLY the provided context. Be concise.",
                },
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
            ],
        )
        answer = resp.choices[0].message.content or ""
        return {
            "schema_version": "1.0.0",
            "system_version": {"pipeline_version": "smoke-0.1.0", "generator_model": gen_model},
            "query": {"query_id": "smoke", "text": question},
            "answer": {"text": answer, "citations": []},
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

    return query_callable


def main() -> None:
    """Run a 2-question golden-set eval through the migrated app, traced to Phoenix."""
    cache_dir = Path(os.environ["GST_CACHE_DIR"])
    slice_name = os.environ.get("SLICE", "gst_pico")
    endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")

    dataset = list(load_gst_legal_rag(cache_dir=cache_dir, slice=slice_name))
    context_by_question = {q_text: gold for (_qid, q_text, _rel, gold) in dataset}
    print(f"[smoke] loaded {len(dataset)} questions from slice={slice_name}")

    adapter = RagAdapter(query_callable=_build_query_callable(context_by_question))

    dc = get_deepeval_config({})  # provider/model resolve from CRUCIBLE_JUDGE_* env
    print(f"[smoke] judge = {dc['judge_model_provider']}/{dc['judge_model']}")
    evaluator = DeepEvalEvaluator(
        metrics=create_deepeval_metrics(
            llm_provider=dc["judge_model_provider"],
            judge_model=dc["judge_model"],
            temperature=dc["temperature"],
        ),
        suppress_tracing=suppress_tracing_if_available,
    )

    phoenix = PhoenixAdapter(endpoint=endpoint, project_name=PHOENIX_PROJECT, enabled=True)
    print(f"[smoke] phoenix project={PHOENIX_PROJECT} connected={phoenix.is_connected()}")

    result = run_golden_set(
        dataset=dataset,
        adapter=adapter,
        evaluator=evaluator,
        config={},
        phoenix=phoenix,
        corpus_dir=cache_dir,
    )

    print(f"\n[smoke] RunResult: success={result.summary.success_count} "
          f"errors={result.summary.error_count}")
    for row in result.rows:
        print(
            f"  q={row.query_id} verdict={row.judge_verdict} "
            f"faith={row.faithfulness_score} ctx_prec={row.context_precision_score} "
            f"ctx_rec={row.context_recall_score} ans_rel={row.answer_relevancy_score}"
        )
    print(f"\n[smoke] view traces at {endpoint} -> Projects -> {PHOENIX_PROJECT}")


if __name__ == "__main__":
    main()
