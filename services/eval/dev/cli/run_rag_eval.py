"""
CLI shell for RAG evaluation on Legal RAG Bench (thin wrapper).

Usage:
    uv run eval-rag --slice full --rag stub-local
    uv run eval-rag --slice pico --rag opensearch

This is a LOCAL/demo shell: it parses args, resolves the judge provider/model and
builds the ``RagAdapter`` (stub-local ChromaDB demo backend, or the OpenSearch
retrieval backend), then runs the Phoenix-native Datasets & Experiments flow via
the service library (``service/runners/golden_set.run_phoenix_native`` ->
``service/phoenix/experiments``). Every run produces a per-question, scored
experiment in the Phoenix UI plus the canonical CSV/parquet/JSON artifacts via
``export_experiment_results``.

A running Phoenix server is REQUIRED for ``eval-rag``; ``eval check phoenix``
is the fail-fast preflight. There is no offline/no-server CLI scoring path.

NOTE: stub-local uses a ChromaDB reference implementation for demonstration only.
``--rag opensearch`` queries an externally owned OpenSearch index (config via
``EVAL_OPENSEARCH_*`` env vars; see ``dev.stubs.rag.opensearch_query``).
The judge runs on AWS Bedrock only.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from beartype import beartype
from dotenv import load_dotenv

# Load environment variables from .env file (Phase 4 owns side-effect cleanup).
load_dotenv()


def load_dataset(slice_name: str, config: dict) -> Any:
    """
    Load a dataset slice via the SINGLE resolve dispatch point.

    Args:
        slice_name: Slice (e.g. 'pico', 'nano', 'full', 'gst_pico').
        config: Configuration dictionary (supplies the config-driven cache path).

    Returns:
        Iterator over dataset query tuples.

    """
    from app.datasets.resolve import resolve_routing

    routing = resolve_routing(slice_name)
    dataset_config = config["datasets"].get(routing.config_key, {})
    cache_dir = Path(dataset_config.get("cache_path", f"data/rag/{routing.config_key}"))
    return routing.loader(cache_dir=cache_dir, slice=slice_name)


@beartype
def get_rag(
    rag_name: str,
    force_reingest: bool = False,
    top_k: int = 5,
    embedder: Any = None,
) -> Any:
    """
    Build the ``RagAdapter`` for the selected backend.

    Backends (all imports are LAZY so each backend works without the other's
    optional extra installed — stub-local needs no opensearch-py, opensearch
    needs no chromadb):
    - ``stub-local``: demonstration ChromaDB backend (``demo`` extra).
    - ``opensearch``: hybrid BM25+k-NN retrieval against an externally owned
      OpenSearch index (``opensearch`` extra; config via ``EVAL_OPENSEARCH_*``
      env vars). ``force_reingest`` is a documented NO-OP here — eval never
      builds or mutates the index (the ingestion service owns it).

    Args:
        rag_name: RAG system name ('stub-local' or 'opensearch').
        force_reingest: Force corpus re-ingestion (stub-local only; no-op for
            opensearch).
        top_k: Number of chunks to retrieve.
        embedder: Optional shared embedder.

    Returns:
        A configured ``RagAdapter``.

    Raises:
        ValueError: On an unknown rag_name.

    """
    from app.kernel.interfaces import RagAdapter

    if rag_name == "opensearch":
        from dev.stubs.rag.opensearch_query import query as opensearch_query

        def opensearch_wrapper(
            question: str, corpus_dir: Path, embedder: Any = None
        ) -> dict[str, Any]:
            # force_reingest is deliberately NOT forwarded: it is a documented
            # no-op for the read-only opensearch backend.
            return opensearch_query(
                question=question,
                corpus_dir=corpus_dir,
                top_k=top_k,
                embedder=embedder,
            )

        return RagAdapter(query_callable=opensearch_wrapper, embedder=embedder)

    if rag_name == "stub-local":
        from dev.stubs.rag.chromadb_query import query as chromadb_query

        def chromadb_wrapper(
            question: str, corpus_dir: Path, embedder: Any = None
        ) -> dict[str, Any]:
            return chromadb_query(
                question=question,
                corpus_dir=corpus_dir,
                top_k=top_k,
                force_reingest=force_reingest,
                embedder=embedder,
            )

        return RagAdapter(query_callable=chromadb_wrapper, embedder=embedder)

    raise ValueError(f"Unknown RAG backend: {rag_name!r} (use 'stub-local' or 'opensearch')")


def _build_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate RAG with DeepEval metrics")
    parser.add_argument(
        "--slice",
        choices=["pico", "nano", "full", "gst_pico", "gst_nano", "gst_mini", "gst_full"],
        default="pico",
    )
    parser.add_argument("--rag", required=True, choices=["stub-local", "opensearch"])
    parser.add_argument("--config", type=Path, default=Path("eval_config.yaml"))
    parser.add_argument("--output-dir", type=Path, default=None)
    # --force-reingest applies to stub-local only; it is a documented no-op for
    # --rag opensearch (eval reads an externally owned index, never builds it).
    parser.add_argument("--force-reingest", action="store_true")
    parser.add_argument("--top-k", type=int, default=5)
    # Phoenix experiment name; defaults to "{dataset}-{slice}". Give each arm of
    # an A/B comparison (e.g. chunking strategies) a distinct name so the runs
    # are tellable apart in the Phoenix experiments table.
    parser.add_argument("--experiment-name", type=str, default=None)
    return parser.parse_args()


def _build_embedder(dataset_config: dict, get_embedder: Any, rag_name: str = "stub-local") -> Any:
    """
    Build the shared embedder for the selected backend.

    Provider resolution: the dataset config's ``embeddings.provider`` wins;
    otherwise ``--rag opensearch`` defaults to ``bedrock`` (Titan V2 — must
    match the index embeddings) and ``stub-local`` keeps ``huggingface``
    (sentence-transformers).

    The ``bedrock`` provider is served by the dev-local Titan embedder class
    (lazy import; boto3 comes from the ``bedrock`` extra) — NOT by the app's
    placeholder BedrockEmbedder.
    """
    cfg = dataset_config.get("embeddings", {})
    default_provider = "bedrock" if rag_name == "opensearch" else "huggingface"
    provider = cfg.get("provider", default_provider)

    if provider == "bedrock":
        from dev.stubs.rag.bedrock_embedder import DEFAULT_EMBEDDING_MODEL, BedrockTitanEmbedder

        return BedrockTitanEmbedder(model_id=cfg.get("model", DEFAULT_EMBEDDING_MODEL))

    return get_embedder(
        provider=provider,
        model=cfg.get("model", "sentence-transformers/all-MiniLM-L6-v2"),
    )


def _default_output_dir(args: Any) -> Path:
    if args.output_dir is not None:
        return args.output_dir
    return Path("results") / "eval_rag" / datetime.now().strftime("%Y%m%d_%H%M%S")


def _phoenix_native(args: Any, config: dict, get_deepeval_config: Any, get_embedder: Any) -> None:
    from app.datasets.resolve import resolve_routing
    from app.phoenix.experiments import export_experiment_results
    from app.runners.golden_set import run_phoenix_native

    routing = resolve_routing(args.slice)
    dataset_config = config["datasets"].get(routing.config_key, {})
    corpus_dir = Path(dataset_config.get("path", f"data/rag/{routing.config_key}/corpus_files"))
    embedder = _build_embedder(dataset_config, get_embedder, rag_name=args.rag)
    rag_adapter = get_rag(args.rag, args.force_reingest, args.top_k, embedder)
    output_dir = _default_output_dir(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    experiment = run_phoenix_native(
        rag_adapter=rag_adapter,
        corpus_dir=corpus_dir,
        endpoint=os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006"),
        slice_name=args.slice,
        experiment_name=args.experiment_name or f"{routing.dataset_name}-{args.slice}",
        judge_model=get_deepeval_config(config)["judge_model"],
    )
    _ = export_experiment_results(experiment, output_dir)
    print(f"Phoenix-native experiment complete; results in {output_dir}")


def main() -> None:
    """Parse args, build injected deps, and run the Phoenix-native experiment."""
    from dev.cli.check import bedrock_preflight
    from app.config import load_config
    from app.deepeval.bedrock_provider import get_deepeval_config
    from app.deepeval.embeddings import get_embedder

    args = _build_args()
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Bedrock startup preflight: fail fast and loud on missing creds / unset
    # region / model access so broken plumbing is never read as a low score.
    try:
        bedrock_preflight()
    except Exception as e:
        print(f"ERROR: Bedrock preflight failed: {e}")
        sys.exit(1)

    # Flow B (Phoenix-native Datasets & Experiments) is the ONLY path. A running
    # Phoenix server is REQUIRED; `eval check phoenix` is the preflight.
    return _phoenix_native(args, config, get_deepeval_config, get_embedder)


if __name__ == "__main__":
    main()
