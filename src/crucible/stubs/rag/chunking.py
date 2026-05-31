"""
Text chunking strategies for RAG document processing.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides configurable
chunking strategies with parameterized chunk sizes and overlap.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChunkingStrategy(Protocol):
    """
    Protocol for text chunking strategies.

    Defines the interface for document chunking implementations.
    All chunking strategies must implement the chunk method.

    Example:
        >>> def use_chunker(chunker: ChunkingStrategy) -> None:
        ...     chunks = chunker.chunk("doc1", "some text")

    """

    def chunk(self, doc_id: str, text: str) -> list[dict]:
        """
        Split a document into chunks.

        Args:
            doc_id: Document identifier for chunk_id generation.
            text: Full document text to chunk.

        Returns:
            List of chunk dictionaries with chunk_id, doc_id, text,
            char_span, and metadata fields.

        """
        ...


class ConfigurableChunker:
    """
    Configurable fixed-size text chunker for document processing.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    The ConfigurableChunker splits documents into deterministic, fixed-size
    chunks based on character count with configurable overlap. This approach
    ensures reproducibility and enables precise character offset tracking.

    Attributes:
        _chunk_size: Number of characters per chunk.
        _chunk_overlap: Number of characters to overlap between chunks.

    Example:
        >>> chunker = ConfigurableChunker(chunk_size=512, chunk_overlap=150)
        >>> chunks = chunker.chunk(doc_id="doc1", text="Hello world. " * 100)
        >>> len(chunks)
        4

    """

    __slots__ = ("_chunk_size", "_chunk_overlap")

    def __init__(self, chunk_size: int, chunk_overlap: int) -> None:
        """
        Initialize the configurable chunker.

        Args:
            chunk_size: Number of characters per chunk. Must be positive.
            chunk_overlap: Number of characters to overlap between chunks.
                Must be non-negative and less than chunk_size.

        Raises:
            ValueError: If chunk_size <= 0 or overlap >= chunk_size.

        """
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")

        if chunk_overlap < 0:
            raise ValueError(f"overlap must be non-negative, got {chunk_overlap}")

        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"overlap must be less than chunk_size, "
                f"got overlap={chunk_overlap}, chunk_size={chunk_size}"
            )

        self._chunk_size: int = chunk_size
        self._chunk_overlap: int = chunk_overlap

    def chunk(self, doc_id: str, text: str) -> list[dict]:
        """
        Split a document into fixed-size chunks.

        Chunks are created by dividing the text into segments of chunk_size
        characters. Each chunk tracks its character offsets (char_span) for
        citation alignment with gold spans.

        Args:
            doc_id: Document identifier for chunk_id generation.
            text: Full document text to chunk.

        Returns:
            List of chunk dictionaries, each containing:
                - chunk_id: Unique identifier (doc_id_chunk_XXXXX)
                - doc_id: Source document identifier
                - text: Chunk text content
                - char_span: [start, end) character offsets
                - metadata: Optional metadata (element_ids, page_indices)

        """
        if not text:
            return []

        chunks = []
        start = 0
        text_length = len(text)
        chunk_idx = 0

        while start < text_length:
            end = min(start + self._chunk_size, text_length)

            chunk = {
                "chunk_id": f"{doc_id}_chunk_{chunk_idx:05d}",
                "doc_id": doc_id,
                "text": text[start:end],
                "char_span": [start, end],
                "metadata": {},
            }

            chunks.append(chunk)

            # Move to next chunk (accounting for overlap)
            start = end - self._chunk_overlap
            chunk_idx += 1

            # Avoid infinite loop when overlap equals chunk size
            if start == end and start < text_length:
                start = end

        return chunks


# Refactored FixedChunker to use ConfigurableChunker internally
class FixedChunker:
    """
    Fixed-size text chunker for backward compatibility.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    This class maintains backward compatibility by wrapping ConfigurableChunker
    with the default configuration values from chromadb_config.

    Attributes:
        _chunker: The underlying ConfigurableChunker instance.

    Example:
        >>> chunker = FixedChunker()
        >>> chunks = chunker.chunk(doc_id="doc1", text="Hello world. " * 100)
        >>> len(chunks)
        3

    """

    __slots__ = ("_chunker",)

    def __init__(self) -> None:
        """Initialize the fixed chunker with configured size and overlap."""
        from crucible.stubs.rag.chromadb_config import CHUNK_OVERLAP, CHUNK_SIZE

        self._chunker: ConfigurableChunker = ConfigurableChunker(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
        )

    def chunk(self, doc_id: str, text: str) -> list[dict]:
        """
        Split a document into fixed-size chunks.

        Args:
            doc_id: Document identifier for chunk_id generation.
            text: Full document text to chunk.

        Returns:
            List of chunk dictionaries with chunk_id, doc_id, text,
            char_span, and metadata fields.

        """
        return self._chunker.chunk(doc_id, text)
