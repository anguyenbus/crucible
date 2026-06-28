"""
Kernel interfaces, protocols, and the concrete RagAdapter (pure).

Holds the concrete ``RagAdapter`` (moved here from ``adapters/rag_adapter.py`` in
Phase 2) plus the structural protocols the monorepo implements later:
``JudgeProvider``, ``RateLimiter`` and the two-phase ``ClaimStore``. Phase 2 wires
only the metrics-dict injection (see
``crucible.kernel.rag_metrics.evaluator.DeepEvalEvaluator``); the protocols are
stubs for the monorepo to satisfy with concrete adapters.

RagAdapter validates output via the kernel schema validator's
``importlib.resources`` default (``schema="rag_query_output"``) -- there is NO
CWD-relative ``Path(...)`` here (Finding A fix on the kernel path).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from crucible.kernel.validation.schema_validator import validate as schema_validate

# Type alias for query callable
QueryCallable = Callable[[str, Path], dict[str, Any]]

# Logical name of the packaged schema RagAdapter validates against.
_RAG_QUERY_OUTPUT_SCHEMA: Final[str] = "rag_query_output"

# Sentinel marking an omitted query_callable so the bare RagAdapter() call can
# raise a fix-pointing TypeError instead of Python's generic missing-arg message.
_MISSING: Final[object] = object()


@runtime_checkable
class JudgeProvider(Protocol):
    """
    Structural protocol for an LLM-judge metric provider.

    A JudgeProvider builds the DeepEval metric objects the kernel evaluator
    consumes. Phase 2 injects an already-built metrics dict into
    ``DeepEvalEvaluator``; the concrete provider implementation (resolving the
    bedrock judge backend) is monorepo work. Defined here so the kernel
    can type against the boundary without importing any infra.
    """

    def build_metrics(self) -> dict[str, Any]:
        """Build and return the metric-name -> metric-object mapping."""
        ...


@runtime_checkable
class RateLimiter(Protocol):
    """
    Structural protocol for a judge-call rate limiter.

    Concrete implementations (token bucket, semaphore over max_concurrent, etc.)
    live in the monorepo. Defined here so the kernel can accept an injected
    limiter without importing infra.
    """

    def acquire(self) -> None:
        """Block until a call slot is available."""
        ...

    def release(self) -> None:
        """Release a previously acquired call slot."""
        ...


@runtime_checkable
class ClaimStore(Protocol):
    """
    Two-phase claim store for idempotent, deduplicated evaluation results.

    A ClaimStore lets concurrent workers coordinate on a repro key so the same
    evaluation is not computed twice:
    - ``claim(repro_key)`` atomically claims the key. It returns a ``Claimed``
      marker when this caller won the claim and should compute the result, or a
      ``CachedResult`` when another caller already finalized it.
    - ``finalize(repro_key, result_uri)`` records the computed result's location
      so subsequent ``claim`` calls return the cached pointer.

    Concrete backends (DynamoDB, local file, etc.) are monorepo work; the kernel
    only depends on this structural boundary.
    """

    def claim(self, repro_key: str) -> Any:
        """Atomically claim ``repro_key``; return Claimed or a CachedResult."""
        ...

    def finalize(self, repro_key: str, result_uri: str) -> None:
        """Record the computed result location for ``repro_key``."""
        ...


class RagAdapter:
    """
    Adapter for RAG systems with schema validation.

    The adapter wraps a query function and validates its output against
    the rag_query_output schema before returning. This ensures that any RAG
    system (stub or real) produces conformant output.

    Attributes:
        query_callable: Function with signature (question, corpus_dir) -> dict.
        embedder: Optional shared embedder instance.

    Example:
        >>> from my_rag.query import query  # any (question, corpus_dir) -> dict callable
        >>> adapter = RagAdapter(query_callable=query)
        >>> output = adapter.query("What is this?", Path("corpus"))
        >>> # output is guaranteed to validate against rag_query_output schema

    """

    def __init__(
        self,
        query_callable: QueryCallable = _MISSING,  # type: ignore[assignment]
        embedder: Any = None,
    ) -> None:
        """
        Initialize RAG adapter.

        Args:
            query_callable: Required query function with signature
                (question: str, corpus_dir: Path) -> dict. The core adapter never
                defaults to a demo backend; demo callers must pass the stub query
                explicitly (any ``(question, corpus_dir) -> dict`` callable;
                demo stubs are available in the standalone crucible repo).
            embedder: Optional shared embedder instance. If provided and the
                query callable accepts an embedder kwarg, it will be passed
                through. This allows sharing embedders between RAG and RAGAS.

        Raises:
            TypeError: If ``query_callable`` is omitted or None.

        """
        # NOTE: query_callable is required so a non-demo context never silently
        # picks up a demo backend. A sentinel default lets
        # a bare RagAdapter() raise this fix-pointing TypeError rather than
        # Python's generic "missing 1 required positional argument" message.
        if query_callable is _MISSING or query_callable is None:
            raise TypeError(
                "query_callable is required; pass a `(question, corpus_dir) -> dict` "
                "callable. Demo stubs are available in the standalone crucible repo."
            )
        self._query = query_callable
        self._embedder = embedder

    def query(self, question: str, corpus_dir: Path) -> dict[str, Any]:
        """
        Query a RAG system and return validated output.

        Invokes the underlying query callable, validates the output against
        rag_query_output.schema.json, and returns the validated dict.

        Args:
            question: The question text to query.
            corpus_dir: Path to document corpus directory.

        Returns:
            Validated RAG query output dictionary conforming to schema.

        Raises:
            SchemaValidationError: If RAG output fails schema validation.

        """
        # Invoke RAG system (pass embedder if callable accepts it)
        import inspect

        sig = inspect.signature(self._query)
        if "embedder" in sig.parameters and self._embedder is not None:
            output = self._query(question, corpus_dir, embedder=self._embedder)
        else:
            output = self._query(question, corpus_dir)

        # Validate against the packaged schema via importlib.resources (no CWD
        # path -- Finding A fix). The logical name resolves inside the wheel.
        schema_validate(output, schema=_RAG_QUERY_OUTPUT_SCHEMA)

        return output
