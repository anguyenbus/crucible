"""Experiments for crucible."""

from crucible.experiments.datasets import (
    DEFAULT_DATASET_NAME,
    create_phoenix_dataset,
    get_phoenix_dataset,
)
from crucible.experiments.deepeval_evaluators import (
    DEFAULT_JUDGE_MODEL,
    create_answer_relevancy_evaluator,
    create_context_precision_evaluator,
    create_context_recall_evaluator,
    create_faithfulness_evaluator,
)
from crucible.experiments.runner import (
    DEFAULT_EXPERIMENT_NAME,
    create_phoenix_client,
    create_rag_task,
    export_experiment_results,
    run_phoenix_experiment,
)

__all__ = [
    # Runner
    "create_phoenix_client",
    "create_rag_task",
    "run_phoenix_experiment",
    "export_experiment_results",
    "DEFAULT_EXPERIMENT_NAME",
    # Datasets
    "create_phoenix_dataset",
    "get_phoenix_dataset",
    "DEFAULT_DATASET_NAME",
    # Evaluators
    "create_faithfulness_evaluator",
    "create_context_precision_evaluator",
    "create_context_recall_evaluator",
    "create_answer_relevancy_evaluator",
    "DEFAULT_JUDGE_MODEL",
]
