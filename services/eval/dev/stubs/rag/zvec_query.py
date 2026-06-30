"""
Zvec-backed RAG query function.

This module implements RAG query using Alibaba Zvec for vector similarity search.
Zvec is a lightweight, in-process vector database ("SQLite of vector databases").
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from rich.console import Console

from dev.stubs.rag.chunker import FixedChunker
from dev.stubs.rag.embedder import SentenceTransformersEmbedder
from dev.stubs.rag.generator import ClaudeGenerator

console = Console()

# Global cached instances
_cached_embedder: SentenceTransformersEmbedder | None = None
_cached_generator: ClaudeGenerator | None = None
_collection_cache: dict[str, tuple[object, list[dict]]] = {}


def _get_embedder() -> SentenceTransformersEmbedder:
    """Get cached embedder instance."""
    global _cached_embedder
    if _cached_embedder is None:
        _cached_embedder = SentenceTransformersEmbedder()
    return _cached_embedder


def _get_generator() -> ClaudeGenerator:
    """Get cached generator instance."""
    global _cached_generator
    if _cached_generator is None:
        _cached_generator = ClaudeGenerator()
    return _cached_generator


def _get_collection_key(corpus_dir: Path, chunk_size: int, chunk_overlap: int) -> str:
    """Generate cache key for Zvec collection."""
    return f"{corpus_dir.stem}_c{chunk_size}_o{chunk_overlap}"


def _build_or_load_collection(
    corpus_dir: Path,
    chunk_size: int,
    chunk_overlap: int,
    embedder: SentenceTransformersEmbedder,
) -> tuple[object, list[dict]]:
    """
    Build or load Zvec collection from cache.

    Args:
        corpus_dir: Path to document corpus.
        chunk_size: Chunk size for text splitting.
        chunk_overlap: Overlap between chunks.
        embedder: Embedder instance.

    Returns:
        Tuple of (Zvec collection, list of chunk dicts).

    """
    collection_key = _get_collection_key(corpus_dir, chunk_size, chunk_overlap)

    # Check cache
    if collection_key in _collection_cache:
        console.print(f"[INFO] Using cached Zvec collection: {collection_key}")
        return _collection_cache[collection_key]

    console.print(f"[INFO] Building Zvec collection for: {collection_key}")
    console.print(f"[INFO] Corpus: {corpus_dir}, chunks: {chunk_size}, overlap: {chunk_overlap}")

    # Import zvec
    try:
        import zvec
    except ImportError as e:
        raise ImportError("zvec package not found. Install with: uv pip install zvec") from e

    # Chunk documents
    chunker = FixedChunker(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = []
    chunk_texts = []

    for file_path in corpus_dir.iterdir():
        if not file_path.is_file() or file_path.suffix != ".txt":
            continue

        with open(file_path) as f:
            text = f.read()

        # Chunk the document
        doc_chunks = chunker.chunk(doc_id=file_path.stem, text=text)

        for doc_chunk in doc_chunks:
            chunks.append(
                {
                    "id": doc_chunk["chunk_id"],
                    "text": doc_chunk["text"],
                    "source": file_path.name,
                }
            )
            chunk_texts.append(doc_chunk["text"])

    file_count = len(list(corpus_dir.iterdir()))
    console.print(f"[INFO] Created {len(chunks)} chunks from {file_count} files")

    # Generate embeddings in batch
    console.print(f"[INFO] Generating embeddings for {len(chunk_texts)} chunks...")
    embed_start = time.time()

    # Process in batches
    batch_size = 100
    all_embeddings = []

    for i in range(0, len(chunk_texts), batch_size):
        batch = chunk_texts[i : i + batch_size]
        batch_embeddings = embedder.embed_batch(batch)
        all_embeddings.extend(batch_embeddings)

        if (i // batch_size + 1) % 10 == 0:
            processed = min(i + batch_size, len(chunk_texts))
            console.print(f"[INFO] Processed {processed}/{len(chunk_texts)} chunks")

    embed_time = time.time() - embed_start
    console.print(f"[INFO] Embeddings generated in {embed_time:.1f}s")

    # Create Zvec collection in temp directory
    console.print(f"[INFO] Creating Zvec collection with {len(all_embeddings)} vectors...")
    embedding_dim = len(all_embeddings[0])

    # Create temp directory for zvec collection
    temp_dir = tempfile.mkdtemp(prefix="zvec_")
    collection_path = Path(temp_dir) / collection_key

    # Define schema for vector collection
    schema = zvec.CollectionSchema(
        name=collection_key,
        fields=[
            zvec.FieldSchema("id", zvec.DataType.STRING),
            zvec.FieldSchema("text", zvec.DataType.STRING),
            zvec.FieldSchema("source", zvec.DataType.STRING),
        ],
        vectors=[
            zvec.VectorSchema(
                name="vector",
                data_type=zvec.DataType.VECTOR_FP32,
                dimension=embedding_dim,
                index_param=zvec.HnswIndexParam(
                    metric_type=zvec.MetricType.COSINE,
                    m=16,
                    ef_construction=200,
                ),
            ),
        ],
    )

    # Create and open collection
    collection = zvec.create_and_open(str(collection_path), schema)

    # Add documents with embeddings
    for chunk, embedding in zip(chunks, all_embeddings, strict=False):
        doc = zvec.Doc(
            id=chunk["id"],
            vectors={"vector": embedding},
            fields={
                "id": chunk["id"],
                "text": chunk["text"],
                "source": chunk["source"],
            },
        )
        collection.insert(doc)

    console.print("[INFO] Zvec collection built")

    # Cache for reuse (keep collection open)
    _collection_cache[collection_key] = (collection, chunks)

    return collection, chunks


def query(
    question: str,
    corpus_dir: Path,
    top_k: int = 5,
    chunk_size: int = 512,
    chunk_overlap: int = 150,
) -> dict:
    """
    Query Zvec-backed RAG system.

    Args:
        question: User question.
        corpus_dir: Path to document corpus.
        top_k: Number of chunks to retrieve.
        chunk_size: Chunk size for text splitting.
        chunk_overlap: Overlap between chunks.

    Returns:
        RAG output with retrieved_chunks, answer, timings.

    """
    total_start = time.perf_counter()

    embedder = _get_embedder()
    generator = _get_generator()

    # Build or load collection
    collection, chunks = _build_or_load_collection(corpus_dir, chunk_size, chunk_overlap, embedder)

    # Retrieve stage
    retrieval_start = time.perf_counter()

    # Embed query
    query_embedding = embedder.embed_query(question)

    # Search Zvec collection
    import zvec

    # Build vector query
    vector_query = zvec.VectorQuery(
        field_name="vector",
        vector=query_embedding,
        param=zvec.HnswQueryParam(ef=300),
    )

    results = collection.query(vector_query, topk=top_k)

    retrieved_chunks = []
    for doc in results:
        retrieved_chunks.append(
            {
                "id": doc.id,
                "text": doc.fields.get("text", ""),
                "source": doc.fields.get("source", ""),
                "score": doc.score or 0.0,
            }
        )

    retrieval_ms = (time.perf_counter() - retrieval_start) * 1000

    # Generation stage
    generation_start = time.perf_counter()

    # Generate answer using retrieved chunks
    generation_result = generator.generate(
        question=question,
        retrieved_chunks=retrieved_chunks,
    )
    answer_text = generation_result.get("text", "")

    generation_ms = (time.perf_counter() - generation_start) * 1000

    return {
        "schema_version": "1.0.0",
        "query": {"text": question},
        "answer": {
            "text": answer_text,
            "answer_supported": True,
            "citations": [],
        },
        "retrieved_chunks": retrieved_chunks,
        "timings_ms": {
            "retrieval": retrieval_ms,
            "generation": generation_ms,
            "total": (time.perf_counter() - total_start) * 1000,
        },
    }


__all__ = ["query"]
