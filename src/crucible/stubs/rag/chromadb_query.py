"""
ChromaDB-backed RAG query function.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module implements the full ChromaDB
RAG pipeline as a demo of the evaluation framework's capabilities.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from crucible.stubs.rag.chromadb_client import ChromaDBManager
from crucible.stubs.rag.chromadb_config import (
    CORPUS_LOADER_VERSION,
    DEFAULT_TOP_K,
    EMBEDDING_MODEL,
    GENERATOR_MODEL,
    PIPELINE_VERSION,
)
from crucible.stubs.rag.citations import extract_citations
from crucible.stubs.rag.generator import ClaudeGenerator
from crucible.stubs.rag.ingestion import DocumentIngester
from crucible.stubs.rag.retriever import SemanticRetriever
from crucible.stubs.rag.schema_conformance import validate_rag_output

console = Console()

# Global cached instances (created once, reused for all queries)
_cached_embedder: Any = None
_cached_generator: Any = None
_external_embedder: Any = None  # Shared embedder from outside (e.g., for RAGAS)


def set_external_embedder(embedder: Any) -> None:
    """Set external embedder to be shared with RAGAS (avoids duplicate model loads)."""
    global _external_embedder
    _external_embedder = embedder


def _get_embedder() -> Any:
    """Get embedder - prefers external shared, falls back to cached."""
    global _external_embedder, _cached_embedder

    # Use shared embedder if available (e.g., from RAGAS)
    if _external_embedder is not None:
        return _external_embedder

    # Otherwise, create and cache local embedder
    if _cached_embedder is None:
        from crucible.stubs.rag.embedder import SentenceTransformersEmbedder

        _cached_embedder = SentenceTransformersEmbedder()
    return _cached_embedder


def _get_generator() -> Any:
    """Get cached generator instance (lazy load, cached after first use)."""
    global _cached_generator
    if _cached_generator is None:
        _cached_generator = ClaudeGenerator()
    return _cached_generator


def query(
    question: str,
    corpus_dir: Path,
    top_k: int = DEFAULT_TOP_K,
    force_reingest: bool = False,
    phoenix_trace_id: str | None = None,
    embedder: Any = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> dict[str, Any]:
    """
    Query the ChromaDB-backed RAG system.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    This function implements the complete RAG pipeline:
    1. Initialize ChromaDB manager and get/create collection
    2. Auto-ingest if collection doesn't exist (with warning)
    3. Generate query embedding
    4. Retrieve top-k chunks using semantic search
    5. Generate answer using Claude
    6. Extract citations
    7. Validate output against schema
    8. Return schema-conformant RAG query output

    Args:
        question: User question to answer.
        corpus_dir: Path to document corpus directory.
        top_k: Number of chunks to retrieve. Default: 5.
        force_reingest: If True, clear and re-ingest the collection. Default: False.
        phoenix_trace_id: Optional Phoenix trace ID for observability. Default: None.
        embedder: Optional shared embedder instance. If provided, used instead of
            creating a new embedder. Useful for sharing with RAGAS to avoid
            duplicate model loads. Default: None (creates local embedder).
        chunk_size: Optional chunk size. If provided, forces re-ingest with this size.
        chunk_overlap: Optional chunk overlap. If provided with chunk_size,
            forces re-ingest with this overlap.

    Returns:
        Dictionary conforming to rag_query_output.schema.json with:
            - schema_version: "1.0.0"
            - system_version: Version info for all components
            - query: Query metadata
            - answer: Generated answer with citations
            - retrieved_chunks: All retrieved chunks in rank order
            - timings_ms: Per-stage latency

    Raises:
        SchemaValidationError: If output fails schema validation.
        ValueError: If API keys are missing or API calls fail.

    """
    total_start = time.perf_counter()

    # Set external embedder if provided (shared with RAGAS)
    if embedder is not None:
        set_external_embedder(embedder)

    # Import classes here to avoid circular imports
    from crucible.stubs.rag.chunker import FixedChunker

    # Generate query_id from question hash
    query_hash = hashlib.md5(question.encode()).hexdigest()[:8]
    query_id = f"chromadb_{query_hash}"

    try:
        # Initialize ChromaDB manager
        manager = ChromaDBManager(persist=True)

        # Collection name derived from corpus directory and chunking config
        # Different chunking configs use separate collections to avoid conflicts
        collection_name = corpus_dir.stem.replace("-", "_").replace("/", "_")
        if chunk_size is not None:
            overlap_suffix = chunk_overlap if chunk_overlap is not None else 0
            collection_name = f"{collection_name}_c{chunk_size}_o{overlap_suffix}"

        # Check if collection exists
        collection_exists = manager.collection_exists(collection_name)

        # Handle force reingest
        if force_reingest and collection_exists:
            console.print(
                f"[INFO] Force reingest requested, deleting collection "
                f"'{collection_name}'"
            )
            manager.delete_collection(collection_name)
            collection_exists = False

        # Auto-create and ingest if needed
        if not collection_exists:
            chunk_info = (
                f" (chunk_size={chunk_size}, overlap={chunk_overlap})"
                if chunk_size
                else ""
            )
            console.print(
                f"[INFO] Collection '{collection_name}' not found{chunk_info}. "
                f"Creating and ingesting from {corpus_dir}"
            )

            collection = manager.get_or_create_collection(collection_name)

            # Initialize ingestion pipeline
            if chunk_size is not None:
                from crucible.stubs.rag.chunking import ConfigurableChunker

                chunker = ConfigurableChunker(
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap if chunk_overlap is not None else 0,
                )
            else:
                chunker = FixedChunker()

            embedder = _get_embedder()
            ingester = DocumentIngester(chunker, embedder)

            # Ingest corpus
            ingester.ingest_corpus(corpus_dir, collection)

        else:
            collection = manager.get_or_create_collection(collection_name)

        # Initialize retriever and generator (use cached instances)
        embedder = _get_embedder()
        retriever = SemanticRetriever(collection, embedder)
        generator = _get_generator()

        # Retrieval stage
        retrieval_start = time.perf_counter()
        retrieved_chunks = retriever.retrieve(question, top_k=top_k)
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000

        # Generation stage
        generation_start = time.perf_counter()
        answer_result = generator.generate(question, retrieved_chunks)
        generation_ms = (time.perf_counter() - generation_start) * 1000

        # Extract citations
        citations = extract_citations(answer_result["text"], retrieved_chunks)

        # Build output
        output = {
            "schema_version": "1.0.0",
            "system_version": {
                "pipeline_version": PIPELINE_VERSION,
                "corpus_loader_version": CORPUS_LOADER_VERSION,
                "embedder_model": EMBEDDING_MODEL,
                "generator_model": GENERATOR_MODEL,
            },
            "query": {
                "query_id": query_id,
                "text": question,
                "metadata": {},
            },
            "answer": {
                "text": answer_result["text"],
                "answer_supported": answer_result["answer_supported"],
                "citations": citations,
            },
            "retrieved_chunks": retrieved_chunks,
            "timings_ms": {
                "retrieval": retrieval_ms,
                "generation": generation_ms,
                "total": (time.perf_counter() - total_start) * 1000,
            },
        }

        # Add trace context if available (optional, for backward compatibility)
        if phoenix_trace_id:
            output["trace"] = {
                "trace_id": phoenix_trace_id,
            }

        # Validate against schema
        validate_rag_output(output)

        return output

    finally:
        # Ensure manager is closed
        if "manager" in locals():
            manager.close()
