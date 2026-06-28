"""
Phoenix experiment runner for RAG evaluation.

Orchestrates running RAG experiments with Phoenix's native experiment API,
including dataset management, task execution, and result retrieval.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from beartype import beartype
from beartype.typing import Callable

from crucible.service.phoenix.evaluators import (
    create_answer_relevancy_evaluator,
    create_context_precision_evaluator,
    create_context_recall_evaluator,
    create_faithfulness_evaluator,
)

try:
    from phoenix.client import Client
    from phoenix.client.resources.datasets import Dataset
    from phoenix.client.resources.experiments.types import (
        RanExperiment as PhoenixRanExperiment,
    )
except ImportError:
    Client = None  # type: ignore[assignment]
    Dataset = None  # type: ignore[assignment]
    PhoenixRanExperiment = None  # type: ignore[assignment]

# Type alias for experiment results (can be dict or Phoenix object)
RanExperiment = PhoenixRanExperiment | dict[str, Any] | None

# Constants
DEFAULT_EXPERIMENT_NAME: Final[str] = "rag-evaluation"

@beartype
def _build_judge(judge_model: str) -> Any:
    """
    Build the AWS Bedrock judge instance for the experiment evaluators.

    DeepEval routes a bare model *string* to its OpenAI ``GPTModel``, so the
    Bedrock judge MUST be passed as an ``AmazonBedrockModel`` *instance*. This
    project is Bedrock-only (OpenAI/gpt-4o removed).
    """
    from crucible.service.deepeval.bedrock_provider import get_deepeval_llm

    return get_deepeval_llm(provider="bedrock", model=judge_model)


@beartype
def create_phoenix_client(
    endpoint: str = "http://localhost:6006",
) -> Client:
    """
    Create a Phoenix client instance.

    Args:
        endpoint: Phoenix server endpoint (UI URL, converted to base_url).

    Returns:
        Phoenix Client instance.

    Raises:
        ImportError: If Phoenix client is not available.

    """
    if Client is None:
        raise ImportError("Phoenix client not available. Install with: pip install arize-phoenix")

    # Phoenix Client uses base_url parameter
    # The endpoint format is typically http://localhost:6006 for UI
    # The API base_url is http://localhost:6006 (same for Phoenix)
    return Client(base_url=endpoint)


@beartype
def create_rag_task(
    rag_adapter: Any,
    corpus_dir: Path,
) -> Callable:
    """
    Create a RAG task function for Phoenix experiments.

    The task function takes a dataset example and returns the RAG output.

    Args:
        rag_adapter: RAG adapter instance.
        corpus_dir: Path to RAG corpus.

    Returns:
        Task function compatible with Phoenix run_experiment().

    """

    def rag_task(example: dict[str, Any] | str) -> dict[str, Any]:
        """
        Execute RAG query for a single example.

        Args:
            example: Dataset example - can be dict with 'input' key, nested dict,
                    or string directly depending on Phoenix format.

        Returns:
            Dict with 'answer' and 'retrieval_context' for Phoenix evaluators.

        """
        # Extract question from various Phoenix formats
        if isinstance(example, str):
            question = example
        elif isinstance(example, dict):
            # Try different possible structures
            val = example.get("input", "")
            if isinstance(val, dict):
                # Nested dict like {"input": {"input": "question"}}
                question = val.get("input", "")
            elif isinstance(val, str):
                question = val
            else:
                question = str(example.get("input", ""))
        else:
            question = str(example)

        # Query RAG system
        result = rag_adapter.query(question, corpus_dir)

        # Extract output in Phoenix format
        output = result.get("answer", {}).get("text", "")
        retrieved_chunks = result.get("retrieved_chunks", [])
        retrieval_context = [chunk.get("text", "") for chunk in retrieved_chunks]

        # Return tuple: Phoenix maps first element to 'output' by default
        # But we need to return a dict for proper mapping
        return {
            "answer": output,
            "retrieval_context": retrieval_context,
        }

    return rag_task


@beartype
def run_phoenix_experiment(
    rag_adapter: Any,
    corpus_dir: Path,
    judge_model: str,
    endpoint: str = "http://localhost:6006",
    slice_name: str = "pico",
    experiment_name: str | None = None,
) -> RanExperiment:
    """
    Run a RAG evaluation experiment using Phoenix's native experiment API.

    Args:
        rag_adapter: RAG adapter instance.
        corpus_dir: Path to RAG corpus.
        endpoint: Phoenix server endpoint.
        slice_name: Dataset slice ("pico", "nano", or "full").
        experiment_name: Name for the experiment.
        judge_model: LLM judge model for DeepEval metrics.

    Returns:
        RanExperiment instance with results.

    Raises:
        ImportError: If Phoenix client is not available.

    """
    client = create_phoenix_client(endpoint)

    # Create or get dataset
    dataset = create_phoenix_dataset(
        client=client,
        corpus_dir=corpus_dir,
        slice_name=slice_name,
    )

    # Create task function
    task = create_rag_task(rag_adapter, corpus_dir)

    # Create evaluators. Build the judge ONCE: for Bedrock ids this returns an
    # AmazonBedrockModel instance (DeepEval treats a bare string as OpenAI).
    judge = _build_judge(judge_model)
    evaluators = [
        create_faithfulness_evaluator(judge_model=judge),
        create_context_precision_evaluator(judge_model=judge),
        create_context_recall_evaluator(judge_model=judge),
        create_answer_relevancy_evaluator(judge_model=judge),
    ]

    # Run experiment
    name = experiment_name or f"{DEFAULT_EXPERIMENT_NAME}-{slice_name}"

    experiment = client.experiments.run_experiment(
        dataset=dataset,
        task=task,
        evaluators=evaluators,
        experiment_name=name,
        experiment_description=f"RAG evaluation on {slice_name} slice",
    )

    return experiment


@beartype
def export_experiment_results(
    experiment: RanExperiment | dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """
    Export experiment results to CSV, JSON, and Parquet.

    Exports scores, costs, and retrieval context from experiment runs.
    Cost data includes application costs (from Phoenix) and judge costs
    (from evaluator metadata).

    Args:
        experiment: RanExperiment dict from Phoenix run_experiment().
        output_dir: Output directory for results.

    Returns:
        Dictionary with paths to exported files including:
            - csv_path: Path to CSV export
            - json_path: Path to JSON summary
            - parquet_path: Path to Parquet export with cost columns

    """
    import pandas as pd
    import polars as pl

    output_dir.mkdir(parents=True, exist_ok=True)

    # Extract experiment data
    exp_id = experiment.get("experiment_id", "")
    dataset_id = experiment.get("dataset_id", "")
    base_name = experiment.get("experiment_name", f"experiment-{exp_id}")

    # Get task_runs and evaluation_runs
    task_runs = experiment.get("task_runs", [])
    evaluation_runs = experiment.get("evaluation_runs", [])

    # Build maps for evaluations, costs, labels, and verdict breakdown
    eval_map: dict[str, dict[str, float]] = {}
    eval_cost_map: dict[str, dict[str, float]] = {}
    eval_label_map: dict[str, dict[str, str]] = {}  # DeepEval verdict labels
    eval_verdicts_map: dict[str, dict[str, str]] = {}  # verdicts as JSON

    def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
        """Get attribute from both dict-like and object types."""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _extract_verdict(v: Any) -> dict[str, Any]:
        """Extract verdict and reason from dict or object."""
        if isinstance(v, dict):
            return {"verdict": v.get("verdict"), "reason": v.get("reason")}
        return {
            "verdict": getattr(v, "verdict", None),
            "reason": getattr(v, "reason", None),
        }

    for eval_run in evaluation_runs:
        run_id = _get_attr(eval_run, "experiment_run_id", "")
        result = _get_attr(eval_run, "result")
        if result and run_id:
            if isinstance(result, list):
                for r in result:
                    name = _get_attr(r, "name", "")
                    score = _get_attr(r, "score")
                    label = _get_attr(r, "label", "")
                    metadata = _get_attr(r, "metadata", {})
                    eval_cost = _get_attr(metadata, "evaluation_cost", None)

                    if name and score is not None:
                        if run_id not in eval_map:
                            eval_map[run_id] = {}
                        eval_map[run_id][name] = float(score)

                    # Store DeepEval verdict label
                    if name and label:
                        if run_id not in eval_label_map:
                            eval_label_map[run_id] = {}
                        eval_label_map[run_id][name] = str(label)

                    # Store verdicts breakdown (individual yes/no/idk verdicts)
                    verdicts = _get_attr(metadata, "verdicts", None)
                    if name and verdicts:
                        if run_id not in eval_verdicts_map:
                            eval_verdicts_map[run_id] = {}
                        verdict_list = [_extract_verdict(v) for v in verdicts]
                        eval_verdicts_map[run_id][name] = json.dumps(
                            verdict_list, separators=(",", ":")
                        )

                    # Store evaluation cost by metric
                    if name and eval_cost is not None:
                        if run_id not in eval_cost_map:
                            eval_cost_map[run_id] = {}
                        metric_cost_key = f"judge_{name}_cost_usd"
                        eval_cost_map[run_id][metric_cost_key] = float(eval_cost)
            else:
                name = _get_attr(result, "name", "")
                score = _get_attr(result, "score")
                label = _get_attr(result, "label", "")
                metadata = _get_attr(result, "metadata", {})
                eval_cost = _get_attr(metadata, "evaluation_cost", None)

                if name and score is not None:
                    if run_id not in eval_map:
                        eval_map[run_id] = {}
                    eval_map[run_id][name] = float(score)

                # Store DeepEval verdict label
                if name and label:
                    if run_id not in eval_label_map:
                        eval_label_map[run_id] = {}
                    eval_label_map[run_id][name] = str(label)

                # Store verdicts breakdown
                verdicts = _get_attr(metadata, "verdicts", None)
                if name and verdicts:
                    if run_id not in eval_verdicts_map:
                        eval_verdicts_map[run_id] = {}
                    verdict_list = [_extract_verdict(v) for v in verdicts]
                    eval_verdicts_map[run_id][name] = json.dumps(
                        verdict_list, separators=(",", ":")
                    )

                if name and eval_cost is not None:
                    if run_id not in eval_cost_map:
                        eval_cost_map[run_id] = {}
                    metric_cost_key = f"judge_{name}_cost_usd"
                    eval_cost_map[run_id][metric_cost_key] = float(eval_cost)

    # Fetch dataset examples to get input/expected/metadata
    example_map: dict[str, dict[str, Any]] = {}
    if dataset_id:
        try:
            from phoenix.client import Client

            client = Client(base_url="http://localhost:6006")
            dataset = client.datasets.get_dataset(dataset=dataset_id)
            for example in dataset.examples:
                ex_id = example.get("id", "")
                example_map[ex_id] = example
        except Exception:
            pass  # Dataset fetch failed, continue without it

    # Build rows with cost columns
    rows = []
    for task_run in task_runs:
        run_id = _get_attr(task_run, "id", "")
        example_id = _get_attr(task_run, "dataset_example_id", "")
        output = _get_attr(task_run, "output", {})
        error = _get_attr(task_run, "error") or ""

        # Extract app cost from Phoenix task run
        app_cost = _get_attr(task_run, "cost", None)
        if app_cost is None:
            # Use None which will become NaN in parquet
            app_cost_usd = None
        else:
            app_cost_usd = float(app_cost)

        # Get example data
        example = example_map.get(example_id, {})
        input_dict = example.get("input", {}) if isinstance(example, dict) else {}
        expected_dict = example.get("output", {}) if isinstance(example, dict) else {}
        metadata = example.get("metadata", {}) if isinstance(example, dict) else {}

        # Extract values
        question = input_dict.get("input", "") if isinstance(input_dict, dict) else ""
        gold_answer = expected_dict.get("expected", "") if isinstance(expected_dict, dict) else ""
        query_id = metadata.get("query_id", "") if isinstance(metadata, dict) else ""
        relevant_passage = (
            metadata.get("relevant_passage_id", "") if isinstance(metadata, dict) else ""
        )

        # Extract generated answer
        if isinstance(output, dict):
            generated_answer = output.get("answer", "")
        else:
            generated_answer = str(output) if output else ""

        # Get evaluation scores, labels, verdict breakdown, and judge costs
        eval_scores = eval_map.get(run_id, {})
        eval_labels = eval_label_map.get(run_id, {})
        eval_verdicts = eval_verdicts_map.get(run_id, {})
        judge_costs = eval_cost_map.get(run_id, {})

        # Sum all judge costs for this row
        judge_cost_usd = sum(judge_costs.values()) if judge_costs else 0.0

        # Calculate total cost
        total_cost_usd = None
        if app_cost_usd is not None:
            total_cost_usd = app_cost_usd + judge_cost_usd

        # Get timing from task_run if available
        total_ms = 0
        if hasattr(task_run, "latency"):
            total_ms = int(getattr(task_run, "latency", 0) or 0)
        elif isinstance(task_run, dict):
            total_ms = int(task_run.get("latency", 0) or 0)

        row = {
            "experiment_id": exp_id,
            "query_id": query_id,
            "question": question,
            "gold_answer": gold_answer,
            "generated_answer": generated_answer,
            "relevant_passage_retrieved": relevant_passage,
            "faithfulness_score": eval_scores.get("faithfulness", 0.0),
            "faithfulness_label": eval_labels.get("faithfulness", ""),
            "faithfulness_verdicts": eval_verdicts.get("faithfulness", ""),
            "context_precision_score": eval_scores.get("context_precision", 0.0),
            "context_precision_label": eval_labels.get("context_precision", ""),
            "context_precision_verdicts": eval_verdicts.get("context_precision", ""),
            "context_recall_score": eval_scores.get("context_recall", 0.0),
            "context_recall_label": eval_labels.get("context_recall", ""),
            "context_recall_verdicts": eval_verdicts.get("context_recall", ""),
            "answer_relevancy_score": eval_scores.get("answer_relevancy", 0.0),
            "answer_relevancy_label": eval_labels.get("answer_relevancy", ""),
            "answer_relevancy_verdicts": eval_verdicts.get("answer_relevancy", ""),
            "total_ms": total_ms,
            "error": error,
            # Cost columns
            "app_cost_usd": app_cost_usd,
            "judge_cost_usd": judge_cost_usd,
            "total_cost_usd": total_cost_usd,
        }

        # Add per-metric judge costs
        row.update(judge_costs)
        rows.append(row)

    results_df = pd.DataFrame(rows)

    # Export to CSV
    csv_path = output_dir / f"{base_name}_results.csv"
    results_df.to_csv(csv_path, index=False)

    # Export to Parquet with Polars (includes cost columns)
    parquet_path = output_dir / f"{base_name}_results.parquet"
    pl_df = pl.from_pandas(results_df)
    pl_df.write_parquet(parquet_path)

    # Export summary to JSON
    json_path = output_dir / f"{base_name}_summary.json"
    summary = _calculate_experiment_summary(experiment, results_df)

    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    return {
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "parquet_path": str(parquet_path),
    }


@beartype
def _calculate_experiment_summary(
    experiment: RanExperiment | dict[str, Any],
    results_df: Any,
) -> dict[str, Any]:
    """Calculate summary statistics from experiment results."""
    metrics = [
        "faithfulness_score",
        "context_precision_score",
        "context_recall_score",
        "answer_relevancy_score",
    ]
    averages = {}

    if not results_df.empty:
        # Filter out error rows
        valid_df = results_df[results_df["error"].isna() | (results_df["error"] == "")]
        if len(valid_df) > 0:
            for metric in metrics:
                if metric in valid_df.columns:
                    avg = valid_df[metric].mean()
                    averages[metric] = round(float(avg), 4)

    # Extract experiment metadata (handle both dict and object)
    if isinstance(experiment, dict):
        experiment_name = experiment.get("experiment_name", "")
        experiment_id = experiment.get("experiment_id", "")
        dataset_id = experiment.get("dataset_id", "")
    else:
        experiment_name = getattr(experiment, "experiment_name", "")
        experiment_id = getattr(experiment, "experiment_id", "")
        dataset_id = getattr(experiment, "dataset_id", "")

    error_count = int(results_df["error"].ne("").sum()) if "error" in results_df.columns else 0

    return {
        "experiment_name": experiment_name,
        "experiment_id": experiment_id,
        "dataset_id": dataset_id,
        "metrics_avg": averages,
        "total_processed": len(results_df),
        "errors": error_count,
    }


# Constants
DEFAULT_DATASET_NAME: Final[str] = "legal-rag-bench"
DEFAULT_GST_DATASET_NAME: Final[str] = "gst-legal-rag"


@beartype
def create_phoenix_dataset(
    client: Client,
    corpus_dir: Path,
    slice_name: str = "pico",
    dataset_name: str | None = None,
) -> Dataset:
    """
    Create or get a Phoenix dataset from Legal RAG Bench or GST Legal RAG.

    Routes to appropriate loader based on slice_name prefix:
    - gst_* slices use GST Legal RAG loader
    - other slices use Legal RAG Bench loader

    Args:
        client: Phoenix client instance.
        corpus_dir: Path to corpus directory.
        slice_name: Dataset slice (e.g., "pico", "nano", "gst_pico", "gst_full").
        dataset_name: Name for the dataset (auto-generated if None).

    Returns:
        Phoenix Dataset instance.

    Raises:
        ImportError: If Phoenix client is not available.
        ValueError: If corpus_dir does not exist.

    """
    if Client is None:
        raise ImportError("Phoenix client not available")

    if not corpus_dir.exists():
        raise ValueError(f"Corpus directory does not exist: {corpus_dir}")

    # Route to the appropriate loader through the SINGLE resolve dispatch point.
    from crucible.service.datasets.resolve import resolve_routing

    routing = resolve_routing(slice_name)
    load_fn = routing.loader
    base_name = routing.dataset_name

    # Load dataset
    dataset = load_fn(cache_dir=corpus_dir, slice=slice_name)

    # Convert to Phoenix format
    # Phoenix internally maps dataset outputs to evaluator's 'expected' parameter
    # So we use 'expected' key in outputs
    inputs = []
    outputs = []
    metadata_list = []

    for query_id, query_text, relevant_passage_id, gold_answer in dataset:
        inputs.append({"input": query_text})
        outputs.append({"expected": gold_answer})
        metadata_list.append(
            {
                "query_id": query_id,
                "relevant_passage_id": relevant_passage_id,
            }
        )

    # Create or get dataset in Phoenix
    name = dataset_name or f"{base_name}-{slice_name}"

    # Try to get existing dataset first
    try:
        existing = client.datasets.get_dataset(dataset=name)
        # Return existing without adding - prevents duplicates on repeated runs
        return existing
    except Exception:
        # Dataset doesn't exist, create new one
        return client.datasets.create_dataset(
            name=name,
            inputs=inputs,
            outputs=outputs,
            metadata=metadata_list,
            input_keys=["input"],
            output_keys=["expected"],
            dataset_description=f"{base_name} {slice_name} slice",
        )


@beartype
def get_phoenix_dataset(
    client: Client,
    slice_name: str = "pico",
) -> Dataset | None:
    """
    Get an existing Phoenix dataset by name.

    Args:
        client: Phoenix client instance.
        slice_name: Dataset slice (e.g., "pico", "nano", "gst_pico", "gst_full").

    Returns:
        Phoenix Dataset instance or None if not found.

    """
    if Client is None:
        return None

    # Route to the appropriate base name through the SINGLE resolve dispatch point.
    from crucible.service.datasets.resolve import resolve_routing

    base_name = resolve_routing(slice_name).dataset_name

    name = f"{base_name}-{slice_name}"

    try:
        return client.datasets.get_dataset(dataset=name)
    except Exception:
        return None
