"""
DeepEval sample transform (pure kernel).

Transforms crucible RAG query output into the DeepEval ``LLMTestCase`` format and
provides a PURE no-op tracing-suppression context manager.

PURITY NOTE: the kernel must never import ``phoenix`` (forbidden by the
import-linter ``kernel-pure`` contract and the kernel grep-gate). The Phoenix
``suppress_tracing`` integration is therefore INJECTED by the impure caller: the
service/observability layer builds a phoenix-aware suppression factory and passes
it into ``DeepEvalEvaluator(metrics=..., suppress_tracing=...)``. When no factory
is injected the evaluator uses ``noop_suppression`` below -- behaviourally
identical to the old "Phoenix not installed" fallback.
"""

from __future__ import annotations

from contextlib import contextmanager

from beartype import beartype
from beartype.typing import Any, Dict, Generator, List


@contextmanager
def noop_suppression() -> Generator[None, None, None]:
    """No-op tracing-suppression context manager (the kernel default)."""
    yield


@beartype
def transform_to_deepeval_sample(
    rag_output: Dict[str, Any],
    reference_answer: str,
) -> Any:
    """
    Transform crucible RAG output to DeepEval LLMTestCase format.

    Maps the crucible RAG query output structure to DeepEval LLMTestCase
    which requires: input, retrieval_context, actual_output, expected_output.

    Args:
        rag_output: Dictionary conforming to legal_rag_bench_query_output.schema.json
            with keys: query (with text), answer (with text), retrieved_chunks.
        reference_answer: Reference answer text from the dataset.

    Returns:
        LLMTestCase instance with DeepEval-compatible format.

    Example:
        >>> rag_output = {
        ...     "query": {"text": "What is the termination clause?"},
        ...     "answer": {"text": "The contract can be terminated..."},
        ...     "retrieved_chunks": [{"text": "Context 1"}, {"text": "Context 2"}]
        ... }
        >>> sample = transform_to_deepeval_sample(rag_output, "Reference answer")
        >>> print(sample.input)
        'What is the termination clause?'

    """
    from deepeval.test_case import LLMTestCase

    # Extract question from query.text -> input
    input_text = rag_output.get("query", {}).get("text", "")

    # Extract retrieved contexts from retrieved_chunks -> retrieval_context
    retrieved_chunks = rag_output.get("retrieved_chunks", [])
    retrieval_context: List[str] = [
        chunk.get("text", "") for chunk in retrieved_chunks if chunk.get("text")
    ]

    # Extract response from answer.text -> actual_output
    actual_output = rag_output.get("answer", {}).get("text", "")

    # Create LLMTestCase
    return LLMTestCase(
        input=input_text,
        retrieval_context=retrieval_context,
        actual_output=actual_output,
        expected_output=reference_answer,
    )
