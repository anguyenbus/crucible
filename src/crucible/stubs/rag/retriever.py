"""
Semantic retrieval for RAG queries.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the SemanticRetriever class
which performs semantic search over ChromaDB collections using query embeddings.
"""

from __future__ import annotations

from typing import Any

# Lazy tracer import to avoid circular dependency
_tracer = None


def _get_tracer():
    """Get global tracer instance for span emission."""
    global _tracer
    if _tracer is None:
        try:
            from crucible.stubs.span_generator.tracer import setup_tracer

            _, _tracer = setup_tracer()
        except (ImportError, Exception):
            pass  # Tracing not available
    return _tracer


class SemanticRetriever:
    """
    Semantic retriever for ChromaDB collections.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    The SemanticRetriever performs top-k semantic search over ChromaDB
    collections using cosine similarity on query embeddings. Results are
    returned in rank order (rank 0 = highest score).

    Attributes:
        _collection: ChromaDB collection to query.
        _embedder: Embedder with .embed() method for query embeddings.

    Example:
        >>> retriever = SemanticRetriever(collection, embedder)
        >>> results = retriever.retrieve("What is this?", top_k=5)
        >>> len(results)
        5

    """

    __slots__ = ("_collection", "_embedder")

    def __init__(self, collection: Any, embedder: Any) -> None:
        """
        Initialize semantic retriever.

        Args:
            collection: ChromaDB collection instance.
            embedder: Any embedder with .embed() method.

        """
        self._collection = collection
        self._embedder = embedder

    def retrieve(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """
        Retrieve top-k chunks for a query using semantic search.

        Args:
            query: Query text to search for.
            top_k: Number of chunks to retrieve. Default: 5.

        Returns:
            List of retrieved chunk dictionaries in rank order, each containing:
                - chunk_id: Unique chunk identifier
                - rank: Rank position (0 = highest score)
                - score: Cosine similarity score
                - retrieval_stage: "initial" for this implementation
                - doc_id: Source document identifier
                - text: Chunk text content
                - char_span: [start, end) character offsets
                - element_ids: Optional element IDs from metadata
                - page_indices: Optional page indices from metadata

        Raises:
            EmbeddingError: If query embedding fails.
            ValueError: If top_k is less than 1.

        """
        tracer = _get_tracer()

        if tracer is None:
            return self._retrieve_internal(query, top_k)

        from openinference.semconv.trace import OpenInferenceSpanKindValues

        RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER

        with tracer.start_as_current_span(
            "chromadb.query", openinference_span_kind=RETRIEVER
        ) as span:
            # Set required OpenInference attributes
            span.set_attribute("input.value", query)

            # Perform retrieval
            chunks = self._retrieve_internal(query, top_k)

            # Set retrieval document attributes
            for i, chunk in enumerate(chunks):
                span.set_attribute(
                    f"retrieval.documents.{i}.document.id", chunk.get("doc_id", "")
                )
                span.set_attribute(
                    f"retrieval.documents.{i}.document.content",
                    chunk.get("text", ""),
                )
                span.set_attribute(
                    f"retrieval.documents.{i}.document.score",
                    float(chunk.get("score", 0.0)),
                )

            return chunks

    def _retrieve_internal(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """
        Internal retrieval implementation without span emission.

        Args:
            query: Query text to search for.
            top_k: Number of chunks to retrieve.

        Returns:
            List of retrieved chunk dictionaries.

        """
        if top_k < 1:
            raise ValueError("top_k must be at least 1")

        if not query:
            return []

        # Generate query embedding (HuggingFaceEmbedder uses embed())
        query_embedding = self._embedder.embed([query])[0]

        # Query ChromaDB collection
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
        )

        # Parse results into schema-conformant format
        retrieved_chunks = []

        if results and results.get("ids") and results["ids"][0]:
            for rank, chunk_id in enumerate(results["ids"][0]):
                # Extract metadata
                metadata = (
                    results["metadatas"][0][rank] if results.get("metadatas") else {}
                )
                doc_id = metadata.get("doc_id", "")
                char_span = metadata.get("char_span", [0, 0])
                element_ids = metadata.get("element_ids", [])
                page_indices = metadata.get("page_indices", [])

                # Extract text and distance score
                text = results["documents"][0][rank] if results.get("documents") else ""
                distance = (
                    results["distances"][0][rank] if results.get("distances") else 0.0
                )

                # Convert distance to similarity score (ChromaDB uses L2 distance)
                # For normalized embeddings, we can use: similarity = 1 - distance
                score = 1.0 - distance

                retrieved_chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "rank": rank,
                        "score": score,
                        "retrieval_stage": "initial",
                        "doc_id": doc_id,
                        "text": text,
                        "char_span": char_span,
                        "element_ids": element_ids if element_ids else [],
                        "page_indices": page_indices if page_indices else [],
                    }
                )

        return retrieved_chunks
