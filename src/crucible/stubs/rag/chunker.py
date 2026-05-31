"""
Text chunking for RAG document processing.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the FixedChunker class
which splits documents into fixed-size chunks for embedding and retrieval.
"""

from __future__ import annotations

from crucible.stubs.rag.chromadb_config import CHUNK_OVERLAP, CHUNK_SIZE


class FixedChunker:
    """
    Fixed-size text chunker for document processing.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    The FixedChunker splits documents into deterministic, fixed-size chunks
    based on character count. This approach ensures reproducibility and
    enables precise character offset tracking for citation alignment.

    Attributes:
        _chunk_size: Number of characters per chunk (512).
        _chunk_overlap: Number of characters to overlap between chunks (0).

    Example:
        >>> chunker = FixedChunker()
        >>> chunks = chunker.chunk(doc_id="doc1", text="Hello world. " * 100)
        >>> len(chunks)
        3

    """

    __slots__ = ("_chunk_size", "_chunk_overlap")

    def __init__(self, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP) -> None:
        """Initialize the fixed chunker with size and overlap."""
        self._chunk_size: int = chunk_size
        self._chunk_overlap: int = chunk_overlap

    def chunk(self, doc_id: str, text: str) -> list[dict]:
        """
        Split a document into fixed-size chunks.

        Chunks are created by dividing the text into segments of CHUNK_SIZE
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
            prev_start = start
            start = end - self._chunk_overlap
            chunk_idx += 1

            # Avoid infinite loop: if we haven't moved forward, we're done
            if start <= prev_start:
                break

        return chunks
