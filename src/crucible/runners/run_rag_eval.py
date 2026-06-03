"""
CLI runner for RAG evaluation on Legal RAG Bench.

Usage:
    uv run eval-rag --slice full --rag stub-local

NOTE: The stub-local RAG option uses a ChromaDB-based reference implementation
for demonstration purposes. It is not intended for production use.

DeepEval LLM-judge metrics (Faithfulness, ContextualPrecision, ContextualRecall,
AnswerRelevancy) are enabled by default. The judge runs on AWS Bedrock by default
(set AWS_REGION + credentials and grant model access); set CRUCIBLE_JUDGE_PROVIDER=
openai with OPENAI_API_KEY to use OpenAI instead.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from beartype import beartype
from dotenv import load_dotenv

# ====================================================================
# SECURITY: DISABLE THIRD-PARTY TELEMETRY
# ====================================================================
# DO NOT REMOVE OR MODIFY. See deepeval_config.py for full explanation.
# This ensures telemetry is disabled even if this module is imported directly.
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"

# Load environment variables from .env file
load_dotenv()


def load_dataset(slice_name: str, config: dict) -> Any:
    """
    Load dataset by slice, routing to appropriate loader.

    Routes to GST Legal RAG loader for gst_* slices, otherwise to
    Legal RAG Bench loader.

    Args:
        slice_name: Slice of dataset (e.g., 'pico', 'nano', 'full', 'gst_pico').
        config: Configuration dictionary.

    Returns:
        Iterator over dataset items.

    """
    # Route to appropriate loader based on slice prefix
    if slice_name.startswith("gst_"):
        from crucible.datasets import load_gst_legal_rag

        dataset_config = config["datasets"].get("gst_legal_rag", {})
        cache_dir = Path(dataset_config.get("cache_path", "data/rag/gst_legal_rag"))

        return load_gst_legal_rag(cache_dir=cache_dir, slice=slice_name)
    else:
        from crucible.datasets import load_legal_rag_bench

        dataset_config = config["datasets"].get("legal_rag_bench", {})
        cache_dir = Path(dataset_config.get("cache_path", "data/rag/legal_rag_bench"))

        return load_legal_rag_bench(cache_dir=cache_dir, slice=slice_name)


@beartype
def get_rag(
    rag_name: str,
    force_reingest: bool = False,
    top_k: int = 5,
    embedder: Any = None,
) -> Any:
    """
    Get RAG adapter by name.

    NOTE: The 'stub-local' option uses a reference ChromaDB implementation
    for demonstration purposes. It is not intended for production use.

    Args:
        rag_name: Name of RAG system ('stub-local' uses ChromaDB-backed system).
        force_reingest: Force re-ingestion of corpus.
        top_k: Number of chunks to retrieve.
        embedder: Optional shared embedder instance.

    Returns:
        RagAdapter instance.

    """
    from crucible.adapters.rag_adapter import RagAdapter
    from crucible.stubs.rag.chromadb_query import query as chromadb_query

    # Wrap in adapter with config
    def chromadb_wrapper(question: str, corpus_dir: Path, embedder: Any = None) -> dict[str, Any]:
        return chromadb_query(
            question=question,
            corpus_dir=corpus_dir,
            top_k=top_k,
            force_reingest=force_reingest,
            embedder=embedder,
        )

    return RagAdapter(query_callable=chromadb_wrapper, embedder=embedder)


@beartype
def _run_phoenix_native(args: Any, config: dict) -> None:
    """
    Run evaluation using Phoenix Native experiment API.

    Args:
        args: Parsed CLI arguments.
        config: Loaded configuration dictionary.

    """
    from pathlib import Path

    from crucible.adapters.embeddings import get_embedder
    from crucible.experiments.runner import (
        export_experiment_results,
        run_phoenix_experiment,
    )
    from crucible.metrics.deepeval_config import get_deepeval_config

    # Create output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path("results") / "eval_rag" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get dataset config based on slice prefix
    if args.slice.startswith("gst_"):
        dataset_key = "gst_legal_rag"
        dataset_name_prefix = "gst-legal-rag"
    else:
        dataset_key = "legal_rag_bench"
        dataset_name_prefix = "legal-rag-bench"

    dataset_config = config["datasets"].get(dataset_key, {})
    corpus_dir = Path(dataset_config.get("path", f"data/rag/{dataset_key}/corpus_files"))

    # Create shared embedder
    embeddings_config = dataset_config.get("embeddings", {})
    embedder_provider = embeddings_config.get("provider", "huggingface")
    embedder_model = embeddings_config.get("model", "sentence-transformers/all-MiniLM-L6-v2")

    embedder = get_embedder(provider=embedder_provider, model=embedder_model)
    print(f"Shared embedder: {embedder_provider}/{embedder_model}")

    # Get RAG adapter
    rag_adapter = get_rag(
        args.rag,
        force_reingest=args.force_reingest,
        top_k=args.top_k,
        embedder=embedder,
    )

    # Get judge model config
    deepeval_config = get_deepeval_config(config)
    judge_model = deepeval_config["judge_model"]

    # Setup OpenInference auto-instrumentation for OpenAI
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()
        print("OpenAI auto-instrumentation enabled")
    except ImportError:
        print("WARN: OpenInference OpenAI instrumentation not available")

    # Get Phoenix endpoint
    phoenix_endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")

    print("Running Phoenix Native experiment...")
    print(f"  Phoenix endpoint: {phoenix_endpoint}")
    print(f"  Dataset slice: {args.slice}")
    print(f"  Judge model: {judge_model}")

    # Run experiment
    experiment = run_phoenix_experiment(
        rag_adapter=rag_adapter,
        corpus_dir=corpus_dir,
        endpoint=phoenix_endpoint,
        slice_name=args.slice,
        experiment_name=f"{dataset_name_prefix}-{args.slice}",
        judge_model=judge_model,
    )

    # Get experiment name from object or dict
    exp_name = getattr(experiment, "experiment_name", experiment.get("experiment_name", "unknown"))
    print(f"Experiment complete: {exp_name}")
    print(
        f"  Duration: {experiment.get('duration_ms', 0) if isinstance(experiment, dict) else 'N/A'}"
    )

    # Export results
    export_result = export_experiment_results(experiment, output_dir)
    print(f"Results exported to: {output_dir}")
    print(f"  CSV: {export_result['csv_path']}")
    print(f"  Parquet: {export_result['parquet_path']}")

    # Show Phoenix UI link
    print(f"\nView experiments at: {phoenix_endpoint}/datasets")


def main() -> None:
    """Run RAG evaluation on Legal RAG Bench dataset."""
    import argparse

    from crucible.adapters.embeddings import get_embedder
    from crucible.config import load_config
    from crucible.metrics.deepeval_config import get_deepeval_config

    parser = argparse.ArgumentParser(
        description="Evaluate RAG systems on Legal RAG Bench with DeepEval metrics"
    )
    parser.add_argument(
        "--slice",
        choices=["pico", "nano", "full", "gst_pico", "gst_nano", "gst_mini", "gst_full"],
        default="pico",
        help=(
            "Dataset slice: "
            "pico=2, nano=10, full=100 (Legal RAG Bench); "
            "gst_pico=2, gst_nano=10, gst_mini=20, gst_full=76 (GST Legal RAG)"
        ),
    )
    parser.add_argument(
        "--rag",
        required=True,
        choices=["stub-local"],
        help=("RAG system to use. Options: stub-local (ChromaDB-backed reference implementation)"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("eval_config.yaml"),
        help="Path to eval_config.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for CSV results (default: results/eval_rag/TIMESTAMP)",
    )
    parser.add_argument(
        "--force-reingest",
        action="store_true",
        help="Force re-ingestion of corpus into ChromaDB",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of chunks to retrieve (default: 5)",
    )
    parser.add_argument(
        "--phoenix-native",
        action="store_true",
        help="Use Phoenix Native experiment API (shows experiments in Phoenix UI)",
    )

    args = parser.parse_args()

    # Load configuration first (needed for both modes)
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Bedrock startup preflight: when the resolved generator provider is bedrock,
    # make ONE cheap bedrock-runtime call to fail fast and loud on missing creds,
    # an unset/mismatched region, or model access not granted -- so a broken
    # Bedrock plumbing path is never mistaken for a low-quality score. No-op when
    # the provider is not bedrock (e.g. an OpenAI opt-in run).
    try:
        from crucible.cli.check import bedrock_preflight

        bedrock_preflight()
    except Exception as e:
        print(f"ERROR: Bedrock preflight failed: {e}")
        sys.exit(1)

    # Phoenix Native mode - use Phoenix experiment API
    if args.phoenix_native:
        return _run_phoenix_native(args, config)

    # Initialize Phoenix adapter (if available)
    phoenix_adapter = None
    phoenix_endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006")
    try:
        from crucible.observability.phoenix_adapter import PhoenixAdapter

        phoenix_adapter = PhoenixAdapter(
            endpoint=phoenix_endpoint,
            project_name="crucible-eval-rag",
            enabled=True,
        )
        if phoenix_adapter.is_connected():
            print(f"Phoenix tracing enabled at {phoenix_endpoint}")
        else:
            print("Phoenix unavailable - traces will be buffered")
    except ImportError:
        print("Phoenix SDK not installed - tracing disabled")
    except Exception as e:
        print(f"Phoenix initialization failed: {e}")

    # Initialize OpenAI instrumentation (if available)
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()
        print("OpenAI instrumentation enabled")
    except ImportError:
        print("OpenAI instrumentation not available")
    except Exception as e:
        print(f"OpenAI instrumentation failed: {e}")

    # Create output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output_dir is None:
        args.output_dir = Path("results") / "eval_rag" / timestamp
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Get dataset config based on slice prefix
    if args.slice.startswith("gst_"):
        dataset_key = "gst_legal_rag"
        dataset_display_name = "GST Legal RAG"
    else:
        dataset_key = "legal_rag_bench"
        dataset_display_name = "Legal RAG Bench"

    # Load dataset
    dataset_config = config["datasets"].get(dataset_key, {})
    print(f"Loading {dataset_display_name} dataset ({args.slice} slice)")
    dataset = load_dataset(args.slice, config)

    corpus_dir = Path(dataset_config.get("path", f"data/rag/{dataset_key}/corpus_files"))

    # Create shared embedder (used by both RAG retrieval and DeepEval)
    try:
        embeddings_config = dataset_config.get("embeddings", {})
        embedder_provider = embeddings_config.get("provider", "huggingface")
        embedder_model = embeddings_config.get("model", "sentence-transformers/all-MiniLM-L6-v2")

        embedder = get_embedder(provider=embedder_provider, model=embedder_model)
        print(f"Shared embedder: {embedder_provider}/{embedder_model}")
    except Exception as e:
        print(f"ERROR: Could not initialize embedder: {e}")
        sys.exit(1)

    # Initialize DeepEval evaluator (always enabled)
    llm_provider = None
    try:
        from crucible.adapters.deepeval_adapter import DeepEvalEvaluator

        deepeval_config = get_deepeval_config(config)
        judge_model = deepeval_config["judge_model"]
        llm_provider = deepeval_config["judge_model_provider"]
        temperature = deepeval_config["temperature"]
        max_concurrent = deepeval_config["max_concurrent"]

        evaluator = DeepEvalEvaluator(
            llm_provider=llm_provider,
            judge_model=judge_model,
            temperature=temperature,
            max_concurrent=max_concurrent,
            embedder=embedder,
        )

        print(f"DeepEval evaluation enabled with {llm_provider}/{judge_model}")
        print(f"Max concurrent evaluations: {max_concurrent}")
    except Exception as e:
        print(f"ERROR: Could not initialize DeepEval evaluator: {e}")
        if llm_provider == "openai":
            print("The OpenAI judge requires OPENAI_API_KEY to be set.")
        else:
            print(
                "The Bedrock judge requires: AWS credentials on the default chain, "
                "AWS_REGION set, granted model access, and the `aiobotocore` package "
                "(installed via the `bedrock` extra: `uv sync --extra bedrock`). "
                "Run `crucible check bedrock` to diagnose."
            )
        sys.exit(1)

    # Get RAG system
    print(f"Using RAG system: {args.rag}")
    if args.rag == "stub-local":
        print("NOTE: Using reference stub-local implementation (demonstration only)")
    if args.force_reingest:
        print("Force reingest enabled")
    print(f"Top-k retrieval: {args.top_k}")

    rag_adapter = get_rag(
        args.rag,
        force_reingest=args.force_reingest,
        top_k=args.top_k,
        embedder=embedder,
    )

    # Create CSV output file
    csv_path = args.output_dir / "results.csv"
    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    fieldnames = [
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
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    writer.writeheader()

    # Process queries
    print(f"\nProcessing {args.slice} slice...")
    success_count = 0
    error_count = 0

    # Count dataset items for eval run span
    dataset_list = list(dataset)
    num_queries = len(dataset_list)

    # Run evaluation within Phoenix eval_run span (if enabled)
    if phoenix_adapter:
        eval_run_ctx = phoenix_adapter.eval_run_span(
            run_name=f"{dataset_key}-{args.slice}",
            num_questions=num_queries,
            metadata={"slice": args.slice, "rag": args.rag, "top_k": args.top_k},
        )
    else:
        # No-op context manager if Phoenix not available
        from contextlib import nullcontext

        eval_run_ctx = nullcontext()

    with eval_run_ctx:
        for query_id, query_text, relevant_passage_id, gold_answer in dataset_list:
            try:
                # RAG query span (if Phoenix enabled)
                if phoenix_adapter:
                    query_ctx = phoenix_adapter.rag_query_span(question=query_text)
                    query_ctx.__enter__()
                else:
                    query_ctx = None

                # Query RAG system
                output = rag_adapter.query(query_text, corpus_dir)

                # Extract fields from output
                retrieved_chunks = output.get("retrieved_chunks", [])
                timings = output.get("timings_ms", {})
                generated_answer = output.get("answer", {}).get("text", "")

                # Check if relevant passage was retrieved
                relevant_passage_retrieved = any(
                    chunk.get("doc_id") == relevant_passage_id for chunk in retrieved_chunks
                )

                # Compute metrics with full reasoning (DeepEval)
                metric_result = evaluator.compute_metrics_with_reasoning(output, gold_answer)

                metric_scores = metric_result["scores"]
                faithfulness = metric_scores.get("faithfulness", 0.0)

                # Determine verdict
                verdict = "PASS" if faithfulness > 0.7 else "NEEDS_REVIEW"

                # Prepare result row
                result = {
                    "query_id": query_id,
                    "question": query_text,
                    "gold_answer": gold_answer,
                    "generated_answer": generated_answer,
                    "relevant_passage_retrieved": relevant_passage_retrieved,
                    "faithfulness_score": faithfulness,
                    "context_precision_score": metric_scores.get("context_precision", 0.0),
                    "context_recall_score": metric_scores.get("context_recall", 0.0),
                    "answer_relevancy_score": metric_scores.get("answer_relevancy", 0.0),
                    "judge_verdict": verdict,
                    "total_ms": timings.get("total", 0),
                    "error": "",
                }

                writer.writerow(result)
                csv_file.flush()
                success_count += 1
                print(f"  [{query_id}] {verdict}")

            except Exception as e:
                error_result = {
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

                writer.writerow(error_result)
                csv_file.flush()
                error_count += 1
                print(f"  [{query_id}] ERROR: {e}")

            finally:
                # Exit RAG query span (if Phoenix enabled)
                if query_ctx is not None:
                    query_ctx.__exit__(None, None, None)

    csv_file.close()

    # Summary
    print("\nEvaluation complete:")
    print(f"  Success: {success_count}")
    print(f"  Errors: {error_count}")
    print(f"  Results saved to: {csv_path}")

    # Note: Phoenix traces are automatically exported via OTLP
    if phoenix_adapter and phoenix_adapter.is_connected():
        print(f"View traces at: {phoenix_endpoint}")


if __name__ == "__main__":
    main()
