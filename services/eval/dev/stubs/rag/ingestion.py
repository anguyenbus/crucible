"""
Document ingestion pipeline for ChromaDB.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the DocumentIngester class
which handles batch ingestion of documents into ChromaDB collections.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress

from crucible.local.stubs.rag.chromadb_config import BATCH_SIZE
from crucible.local.stubs.rag.chunking import ChunkingStrategy

console = Console()


class DocumentIngester:
    """
    Batch document ingestion pipeline for ChromaDB.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    The DocumentIngester handles chunking, embedding, and batch upsert
    of documents into ChromaDB collections with progress tracking.

    Attributes:
        _chunker: ChunkingStrategy implementation for text chunking.
        _embedder: Embedder with .embed() method for embedding generation.

    Example:
        >>> ingester = DocumentIngester(chunker, embedder)
        >>> stats = ingester.ingest_corpus(
        ...     corpus_dir=Path("corpus"),
        ...     collection=collection
        ... )
        >>> print(stats["chunks_created"])

    """

    __slots__ = ("_chunker", "_embedder")

    def __init__(self, chunker: ChunkingStrategy, embedder: Any) -> None:
        """
        Initialize document ingester.

        Args:
            chunker: Any chunker implementing ChunkingStrategy protocol.
            embedder: Any embedder with .embed() method.

        """
        self._chunker = chunker
        self._embedder = embedder

    def ingest_corpus(
        self,
        corpus_dir: Path,
        collection: Any,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, int]:
        """
        Ingest a corpus of documents into ChromaDB collection.

        Processes all .txt files in the corpus directory by:
        1. Reading each document
        2. Chunking into fixed-size segments
        3. Generating embeddings for chunks
        4. Batch upserting to ChromaDB

        Args:
            corpus_dir: Path to corpus directory containing .txt files.
            collection: ChromaDB collection to upsert into.
            progress_callback: Optional callback(current, total) for progress updates.

        Returns:
            Dictionary with ingestion statistics:
                - documents_processed: Number of documents ingested
                - chunks_created: Total number of chunks created
                - errors: Number of errors encountered

        """
        if not corpus_dir.exists():
            console.print(f"[WARNING] Corpus directory not found: {corpus_dir}")
            return {"documents_processed": 0, "chunks_created": 0, "errors": 0}

        # Find all .txt files
        txt_files = list(corpus_dir.glob("**/*.txt"))

        if not txt_files:
            console.print(f"[WARNING] No .txt files found in: {corpus_dir}")
            return {"documents_processed": 0, "chunks_created": 0, "errors": 0}

        stats = {
            "documents_processed": 0,
            "chunks_created": 0,
            "errors": 0,
        }

        # Pre-embedding optimization: collect all chunks, then batch embed
        # Faster than letting ChromaDB call embedding function per-chunk
        all_chunks: list[dict[str, Any]] = []

        with Progress() as progress:
            task = progress.add_task(
                "[green]Chunking documents...",
                total=len(txt_files),
            )

            for doc_path in txt_files:
                try:
                    # Read document
                    doc_id = doc_path.stem
                    with open(doc_path, encoding="utf-8") as f:
                        text = f.read()

                    # Chunk document
                    chunks = self._chunker.chunk(doc_id=doc_id, text=text)

                    # Store chunks with metadata for later embedding
                    for chunk in chunks:
                        chunk["source_file"] = str(doc_path)
                        all_chunks.append(chunk)

                    stats["documents_processed"] += 1
                    stats["chunks_created"] += len(chunks)

                    # Progress callback
                    if progress_callback:
                        progress_callback(stats["documents_processed"], len(txt_files))

                except Exception:
                    stats["errors"] += 1

                progress.advance(task)

        # Batch embed all chunks at once (much faster!)
        if all_chunks:
            progress_task = console.status("[bold yellow]Generating embeddings for all chunks...")
            progress_task.start()

            try:
                chunk_texts = [c["text"] for c in all_chunks]
                embeddings = self._embedder.embed(chunk_texts)
            finally:
                progress_task.stop()

            # Batch upsert to ChromaDB with pre-computed embeddings
            console.print(f"[bold yellow]Upserting {len(all_chunks)} chunks to ChromaDB...")

            # Process in smaller batches for ChromaDB upsert (avoids memory issues)
            for i in range(0, len(all_chunks), BATCH_SIZE):
                batch_end = min(i + BATCH_SIZE, len(all_chunks))
                batch_chunks = all_chunks[i:batch_end]
                batch_embeddings = embeddings[i:batch_end]

                collection.upsert(
                    ids=[c["chunk_id"] for c in batch_chunks],
                    embeddings=batch_embeddings,
                    documents=[c["text"] for c in batch_chunks],
                    metadatas=[
                        {
                            "doc_id": c["doc_id"],
                            "char_span": c["char_span"],
                            "source_file": c["source_file"],
                        }
                        for c in batch_chunks
                    ],
                )

        console.print(
            f"[INFO] Ingestion complete: {stats['documents_processed']} documents, "
            f"{stats['chunks_created']} chunks, {stats['errors']} errors"
        )

        return stats
