"""
DeepEval metrics wrapped as Phoenix evaluators.

Uses @create_evaluator decorator to make DeepEval metrics compatible with
Phoenix's run_experiment() API. This preserves DeepEval's LLM-judge quality
while gaining Phoenix's experiment tracking, comparison UI, and schema.

Example:
    >>> from phoenix.client import Client
    >>> from crucible.service.phoenix.evaluators import (
    ...     create_faithfulness_evaluator,
    ...     create_context_precision_evaluator,
    ... )
    >>>
    >>> client = Client(endpoint="http://localhost:6006")
    >>> dataset = client.datasets.create_dataset(...)
    >>>
    >>> experiment = client.experiments.run_experiment(
    ...     dataset=dataset,
    ...     task=rag_task,
    ...     evaluators=[
    ...         create_faithfulness_evaluator(),
    ...         create_context_precision_evaluator(),
    ...     ],
    ... )

"""

from __future__ import annotations

from typing import Any, Final

from beartype import beartype
from beartype.typing import Callable

# SECURITY (telemetry ordering): this module imports ``deepeval.metrics`` lazily
# inside its evaluator factories, but it is NOT inside an allowlisted opt-out
# package. Importing ``crucible.service.deepeval`` here runs that package's
# ``__init__`` (which sets ``DEEPEVAL_TELEMETRY_OPT_OUT="YES"``) at module load,
# guaranteeing the opt-out fires before any lazy ``deepeval`` import below —
# structurally, not by caller ordering. Do NOT remove.
import crucible.service.deepeval as _deepeval_optout  # noqa: F401

# Phoenix evaluator decorator
try:
    from phoenix.evals import create_evaluator
except ImportError:
    create_evaluator = None  # type: ignore[assignment]

# Constants
DEFAULT_JUDGE_MODEL: Final[str] = "gpt-4o-mini"


@beartype
def _serialize_verdicts(verdicts: Any) -> list[dict[str, Any]]:
    """
    Serialize DeepEval verdict objects to dict safely across pydantic versions.

    Handles pydantic v2 (model_dump), pydantic v1 (dict), and plain objects.

    Args:
        verdicts: Verdict objects from DeepEval metrics.

    Returns:
        List of serialized verdict dicts.

    """
    if not verdicts:
        return []

    result = []
    for v in verdicts:
        if hasattr(v, "model_dump"):  # pydantic v2
            result.append(v.model_dump())
        elif hasattr(v, "dict"):  # pydantic v1
            result.append(v.dict())
        else:
            result.append(vars(v))  # plain dataclass / object
    return result


@beartype
def _suppress_tracing_if_available() -> Any:
    """
    Get Phoenix suppress_tracing context manager if available.

    Prevents DeepEval LLM judge calls from creating noisy child spans.

    """
    try:
        from phoenix.core.tracing import suppress_tracing

        return suppress_tracing()
    except (ImportError, AttributeError):
        # Return no-op context manager
        from contextlib import contextmanager

        @contextmanager
        def _noop_context():
            yield

        return _noop_context()


@beartype
def create_faithfulness_evaluator(
    judge_model: str = DEFAULT_JUDGE_MODEL,
    embedder: Any = None,
) -> Callable:
    """
    Create a Phoenix evaluator for DeepEval Faithfulness metric.

    Measures hallucination by verifying that generated claims are supported
    by retrieved context.

    Args:
        judge_model: Judge model name.
        embedder: Unused (kept for API compatibility).

    Returns:
        Phoenix evaluator function compatible with run_experiment().

    """
    if create_evaluator is None:
        raise ImportError("phoenix.evals.create_evaluator not available")

    from deepeval.metrics import FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    @create_evaluator(name="faithfulness", kind="llm")
    def faithfulness_evaluator(
        output: dict[str, Any] | None,
        expected: str | dict[str, str] | None = None,
        input: str | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate faithfulness using DeepEval.

        Args:
            output: Task output dict with 'answer' and 'retrieval_context' keys.
            expected: Reference answer (unused by faithfulness).
            input: Original input question (string or dict with 'input' key).

        Returns:
            Dict with score, label, and explanation.

        """
        # Extract input string (handle both string and dict formats)
        if isinstance(input, dict):
            input_str = input.get("input", "") or input.get("expected", "")
        else:
            input_str = input or ""

        # Extract values from task output dict
        if isinstance(output, dict):
            actual_answer = output.get("answer", "")
            retrieval_context = output.get("retrieval_context", [])
        else:
            actual_answer = str(output) if output else ""
            retrieval_context = []

        # Guard: faithfulness requires retrieval context
        if not retrieval_context or not actual_answer:
            return {
                "score": 0.0,
                "label": "skipped",
                "explanation": (
                    "Missing retrieval_context or actual_output; faithfulness cannot be computed."
                ),
            }

        # Create metric instance per call (thread-safety for concurrent eval)
        metric = FaithfulnessMetric(
            model=judge_model,
            include_reason=True,
        )

        # Create DeepEval test case
        # Note: Faithfulness doesn't use expected_output
        test_case = LLMTestCase(
            input=input_str,
            actual_output=actual_answer,
            retrieval_context=retrieval_context,
        )

        # Measure with trace suppression
        with _suppress_tracing_if_available():
            metric.measure(test_case)

        # Build metadata with verdicts for analysis
        metadata: dict[str, Any] = {
            "threshold": metric.threshold,
            "success": metric.success,
            "model": getattr(metric, "evaluation_model", None),
            "evaluation_cost": getattr(metric, "evaluation_cost", None),
        }

        # Include verdicts for all results (pass or fail)
        if hasattr(metric, "verdicts"):
            metadata["verdicts"] = _serialize_verdicts(metric.verdicts)

        from phoenix.evals.evaluators import Score

        return Score(
            name="faithfulness",
            score=float(metric.score),
            label="faithful" if metric.success else "unfaithful",
            explanation=metric.reason or "",
            metadata=metadata,
        )

    return faithfulness_evaluator


@beartype
def create_context_precision_evaluator(
    judge_model: str = DEFAULT_JUDGE_MODEL,
    embedder: Any = None,
) -> Callable:
    """
    Create a Phoenix evaluator for DeepEval ContextualPrecision metric.

    Measures signal-to-noise ratio in retrieved context by counting relevant
    vs irrelevant chunks.

    Args:
        judge_model: Judge model name.
        embedder: Unused (kept for API compatibility).

    Returns:
        Phoenix evaluator function compatible with run_experiment().

    """
    if create_evaluator is None:
        raise ImportError("phoenix.evals.create_evaluator not available")

    from deepeval.metrics import ContextualPrecisionMetric
    from deepeval.test_case import LLMTestCase

    @create_evaluator(name="context_precision", kind="llm")
    def context_precision_evaluator(
        output: dict[str, Any] | None,
        expected: str | dict[str, str],
        input: str | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate contextual precision using DeepEval.

        Args:
            output: Task output dict with 'answer' and 'retrieval_context' keys.
            expected: Reference answer (string or dict with 'expected'/'output' key).
            input: Original input question (string or dict with 'input' key).

        Returns:
            Dict with score, label, and explanation.

        """
        # Extract input string
        if isinstance(input, dict):
            input_str = input.get("input", "") or input.get("expected", "")
        else:
            input_str = input or ""

        # Extract expected string
        if isinstance(expected, dict):
            expected_str = expected.get("expected", "") or expected.get("output", "")
        else:
            expected_str = expected or ""

        # Extract values from task output dict
        if isinstance(output, dict):
            actual_answer = output.get("answer", "")
            retrieval_context = output.get("retrieval_context", [])
        else:
            actual_answer = str(output) if output else ""
            retrieval_context = []

        # Guard: need retrieval context and expected answer
        if not retrieval_context or not expected_str:
            return {
                "score": 0.0,
                "label": "skipped",
                "explanation": (
                    "Missing retrieval_context or expected answer; "
                    "contextual precision cannot be computed."
                ),
            }

        # Create metric instance per call (thread-safety for concurrent eval)
        metric = ContextualPrecisionMetric(
            model=judge_model,
            include_reason=True,
        )

        test_case = LLMTestCase(
            input=input_str,
            actual_output=actual_answer,
            retrieval_context=retrieval_context,
            expected_output=expected_str,
        )

        with _suppress_tracing_if_available():
            metric.measure(test_case)

        # Build metadata with verdicts for analysis
        metadata: dict[str, Any] = {
            "threshold": metric.threshold,
            "success": metric.success,
            "model": getattr(metric, "evaluation_model", None),
            "evaluation_cost": getattr(metric, "evaluation_cost", None),
        }

        # Include verdicts for all results (pass or fail)
        if hasattr(metric, "verdicts"):
            metadata["verdicts"] = _serialize_verdicts(metric.verdicts)

        from phoenix.evals.evaluators import Score

        return Score(
            name="context_precision",
            score=float(metric.score),
            label="precise" if metric.success else "imprecise",
            explanation=metric.reason or "",
            metadata=metadata,
        )

    return context_precision_evaluator


@beartype
def create_context_recall_evaluator(
    judge_model: str = DEFAULT_JUDGE_MODEL,
    embedder: Any = None,
) -> Callable:
    """
    Create a Phoenix evaluator for DeepEval ContextualRecall metric.

    Measures coverage by checking if expected answer information is present
    in retrieved context.

    Args:
        judge_model: Judge model name.
        embedder: Unused (kept for API compatibility).

    Returns:
        Phoenix evaluator function compatible with run_experiment().

    """
    if create_evaluator is None:
        raise ImportError("phoenix.evals.create_evaluator not available")

    from deepeval.metrics import ContextualRecallMetric
    from deepeval.test_case import LLMTestCase

    @create_evaluator(name="context_recall", kind="llm")
    def context_recall_evaluator(
        output: dict[str, Any] | None,
        expected: str | dict[str, str],
        input: str | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate contextual recall using DeepEval.

        Args:
            output: Task output dict with 'answer' and 'retrieval_context' keys.
            expected: Reference answer (string or dict with 'expected'/'output' key).
            input: Original input question (string or dict with 'input' key).

        Returns:
            Dict with score, label, and explanation.

        """
        # Extract input string
        if isinstance(input, dict):
            input_str = input.get("input", "") or input.get("expected", "")
        else:
            input_str = input or ""

        # Extract expected string
        if isinstance(expected, dict):
            expected_str = expected.get("expected", "") or expected.get("output", "")
        else:
            expected_str = expected or ""

        # Extract values from task output dict
        if isinstance(output, dict):
            actual_answer = output.get("answer", "")
            retrieval_context = output.get("retrieval_context", [])
        else:
            actual_answer = str(output) if output else ""
            retrieval_context = []

        # Guard: need retrieval context and expected answer
        if not retrieval_context or not expected_str:
            return {
                "score": 0.0,
                "label": "skipped",
                "explanation": (
                    "Missing retrieval_context or expected answer; "
                    "contextual recall cannot be computed."
                ),
            }

        # Create metric instance per call (thread-safety for concurrent eval)
        metric = ContextualRecallMetric(
            model=judge_model,
            include_reason=True,
        )

        test_case = LLMTestCase(
            input=input_str,
            actual_output=actual_answer,
            retrieval_context=retrieval_context,
            expected_output=expected_str,
        )

        with _suppress_tracing_if_available():
            metric.measure(test_case)

        # Build metadata with verdicts for analysis
        metadata: dict[str, Any] = {
            "threshold": metric.threshold,
            "success": metric.success,
            "model": getattr(metric, "evaluation_model", None),
            "evaluation_cost": getattr(metric, "evaluation_cost", None),
        }

        # Include verdicts for all results (pass or fail)
        if hasattr(metric, "verdicts"):
            metadata["verdicts"] = _serialize_verdicts(metric.verdicts)

        from phoenix.evals.evaluators import Score

        return Score(
            name="context_recall",
            score=float(metric.score),
            label="high_recall" if metric.success else "low_recall",
            explanation=metric.reason or "",
            metadata=metadata,
        )

    return context_recall_evaluator


@beartype
def create_answer_relevancy_evaluator(
    judge_model: str = DEFAULT_JUDGE_MODEL,
    embedder: Any = None,
) -> Callable:
    """
    Create a Phoenix evaluator for DeepEval AnswerRelevancy metric.

    Measures how directly the generated response addresses the input question.

    Args:
        judge_model: Judge model name.
        embedder: Unused (kept for API compatibility).

    Returns:
        Phoenix evaluator function compatible with run_experiment().

    """
    if create_evaluator is None:
        raise ImportError("phoenix.evals.create_evaluator not available")

    from deepeval.metrics import AnswerRelevancyMetric
    from deepeval.test_case import LLMTestCase

    @create_evaluator(name="answer_relevancy", kind="llm")
    def answer_relevancy_evaluator(
        output: dict[str, Any] | None,
        input: str | dict[str, str],
        expected: str | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate answer relevancy using DeepEval.

        Args:
            output: Task output dict with 'answer' key.
            input: Original input question (string or dict with 'input' key).
            expected: Reference answer (optional, unused by relevancy).

        Returns:
            Dict with score, label, and explanation.

        """
        # Extract input string
        if isinstance(input, dict):
            input_str = input.get("input", "") or input.get("expected", "")
        else:
            input_str = input or ""

        # Extract values from task output dict
        if isinstance(output, dict):
            actual_answer = output.get("answer", "")
        else:
            actual_answer = str(output) if output else ""

        # Guard: need input and actual output
        if not input_str or not actual_answer:
            return {
                "score": 0.0,
                "label": "skipped",
                "explanation": (
                    "Missing input or actual_output; answer relevancy cannot be computed."
                ),
            }

        # Create metric instance per call (thread-safety for concurrent eval)
        metric = AnswerRelevancyMetric(
            model=judge_model,
            include_reason=True,
        )

        test_case = LLMTestCase(
            input=input_str,
            actual_output=actual_answer,
        )

        with _suppress_tracing_if_available():
            metric.measure(test_case)

        # Build metadata with verdicts for analysis
        metadata: dict[str, Any] = {
            "threshold": metric.threshold,
            "success": metric.success,
            "model": getattr(metric, "evaluation_model", None),
            "evaluation_cost": getattr(metric, "evaluation_cost", None),
        }

        # Include verdicts for all results (pass or fail)
        if hasattr(metric, "verdicts"):
            metadata["verdicts"] = _serialize_verdicts(metric.verdicts)

        from phoenix.evals.evaluators import Score

        return Score(
            name="answer_relevancy",
            score=float(metric.score),
            label="relevant" if metric.success else "irrelevant",
            explanation=metric.reason or "",
            metadata=metadata,
        )

    return answer_relevancy_evaluator
