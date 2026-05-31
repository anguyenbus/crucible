"""
HTTP endpoints for stub RAG service.

This module defines the FastAPI routes for the stub service, including
health check and query endpoints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

# Router
router = APIRouter()


# Request/Response Models
class QueryRequest(BaseModel):
    """Request model for POST /query."""

    question: str = Field(..., description="User question to answer")
    top_k: int = Field(
        default=5, ge=1, le=20, description="Number of chunks to retrieve"
    )
    trace_context: dict[str, Any] | None = Field(
        default=None, description="Trace context metadata"
    )


class QueryResponse(BaseModel):
    """Response model for POST /query."""

    retrieved_contexts: list[str] = Field(
        ..., description="Retrieved chunk texts in rank order"
    )
    response: dict[str, str] = Field(
        ..., description="Generated response with text field"
    )
    timings_ms: dict[str, float] = Field(
        ..., description="Timings for retrieval, generation, total"
    )


class ErrorResponse(BaseModel):
    """Error response model."""

    error: str = Field(..., description="Error message")
    error_type: str = Field(..., description="Error type/class")
    retryable: bool = Field(default=False, description="Whether request is retryable")


# Endpoints
@router.get("/health")
async def health_check() -> dict[str, str]:
    """
    Health check endpoint.

    Returns:
        {"status": "ok"} if service is healthy.

    """
    return {"status": "ok"}


@router.get("/query")
async def query_get_info() -> dict[str, Any]:
    """
    GET /query returns usage information.

    The actual query endpoint accepts POST requests only.
    """
    return {
        "message": "POST requests only",
        "usage": (
            "curl -X POST http://127.0.0.1:8081/query "
            "-H 'Content-Type: application/json' "
            "-d '{\"question\":\"your question\",\"top_k\":5}'"
        ),
        "contract": "See docs/http-contract.md for full API specification",
    }


@router.post("/query")
async def query_endpoint(request: QueryRequest, http_request: Request) -> QueryResponse:
    """
    Execute a RAG query.

    This endpoint wraps the ChromaDB RAG pipeline behind an HTTP boundary.

    Args:
        request: Query request with question and top_k.
        http_request: FastAPI Request object for accessing app state.

    Returns:
        QueryResponse with retrieved contexts, response text, and timings.

    Raises:
        HTTPException: On malformed input (400) or internal error (500).

    """
    # Get config and tracer from app state
    config = http_request.app.state.config
    tracer = http_request.app.state.tracer
    export_spans = http_request.app.state.export_spans

    # If span export is enabled but tracer is not initialized, return error
    if export_spans and tracer is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "Tracer not initialized",
                "error_type": "ConfigurationError",
                "retryable": False,
            },
        )

    try:
        if export_spans and tracer is not None:
            # Create CHAIN span for HTTP request boundary
            return await _query_with_span(request, config, tracer)
        else:
            # Evaluation mode: no span creation, just execute query
            return await _query_without_span(request, config)

    except ValueError as e:
        # Malformed input
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": str(e),
                "error_type": "ValidationError",
                "retryable": False,
            },
        ) from e

    except Exception as e:
        # Internal error
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": f"Internal error: {e}",
                "error_type": type(e).__name__,
                "retryable": True,
            },
        ) from e


async def _query_with_span(
    request: QueryRequest,
    config: Any,
    tracer: Any,
) -> QueryResponse:
    """
    Execute RAG query with OpenInference span creation.

    Args:
        request: Query request with question and top_k.
        config: Stub configuration.
        tracer: OpenInference tracer instance.

    Returns:
        QueryResponse with retrieved contexts, response text, and timings.

    """
    from openinference.semconv.trace import OpenInferenceSpanKindValues

    from crucible.stubs.rag.chromadb_query import query
    from crucible.stubs.span_generator.span_schema import (
        INPUT_VALUE,
        LATENCY_GENERATION_MS,
        LATENCY_RETRIEVAL_MS,
        LATENCY_TOTAL_MS,
        OUTPUT_VALUE,
        SESSION_ID,
    )

    # Create CHAIN span for HTTP request boundary
    with tracer.start_as_current_span(
        name="synthetic_rag_query",
        openinference_span_kind=OpenInferenceSpanKindValues.CHAIN,
    ) as span:
        import time
        import uuid

        # Set session and input attributes
        session_id = f"http-{time.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:8]}"
        span.set_attribute(INPUT_VALUE, request.question)
        span.set_attribute(SESSION_ID, session_id)

        # Execute RAG query (child spans auto-instrumented)
        result = query(
            question=request.question,
            corpus_dir=config.resolved_corpus_path,
            top_k=request.top_k,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        )

        # Set output and timing attributes
        answer_text = result.get("answer", {}).get("text", "")
        span.set_attribute(OUTPUT_VALUE, answer_text)

        timings = result.get("timings_ms", {})
        span.set_attribute(LATENCY_RETRIEVAL_MS, timings.get("retrieval", 0.0))
        span.set_attribute(LATENCY_GENERATION_MS, timings.get("generation", 0.0))
        span.set_attribute(LATENCY_TOTAL_MS, timings.get("total", 0.0))

        # Extract retrieved context texts
        retrieved_contexts = [
            chunk.get("text", "") for chunk in result.get("retrieved_chunks", [])
        ]

        # Build response matching HTTP contract
        return QueryResponse(
            retrieved_contexts=retrieved_contexts,
            response={"text": answer_text},
            timings_ms=timings,
        )


async def _query_without_span(
    request: QueryRequest,
    config: Any,
) -> QueryResponse:
    """
    Execute RAG query without span creation (evaluation mode).

    Args:
        request: Query request with question and top_k.
        config: Stub configuration.

    Returns:
        QueryResponse with retrieved contexts, response text, and timings.

    """
    # Route to appropriate backend
    if config.retrieval_backend == "zvec":
        from crucible.stubs.rag.zvec_query import query as zvec_query

        result = zvec_query(
            question=request.question,
            corpus_dir=config.resolved_corpus_path,
            top_k=request.top_k,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        )
    else:
        # Default to ChromaDB
        from crucible.stubs.rag.chromadb_query import query

        result = query(
            question=request.question,
            corpus_dir=config.resolved_corpus_path,
            top_k=request.top_k,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        )

    # Extract answer and timings
    answer_text = result.get("answer", {}).get("text", "")
    timings = result.get("timings_ms", {})

    # Extract retrieved context texts
    retrieved_contexts = [
        chunk.get("text", "") for chunk in result.get("retrieved_chunks", [])
    ]

    # Build response matching HTTP contract
    return QueryResponse(
        retrieved_contexts=retrieved_contexts,
        response={"text": answer_text},
        timings_ms=timings,
    )


__all__ = ["router"]
