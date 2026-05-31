"""RAG stub implementations for crucible."""

from crucible.stubs.rag.citations import extract_citations
from crucible.stubs.rag.chunker import FixedChunker
from crucible.stubs.rag.chunking import ChunkingStrategy, ConfigurableChunker, FixedChunker as FixedChunkerV2
from crucible.stubs.rag.chromadb_client import ChromaDBManager
from crucible.stubs.rag.chromadb_config import (
    BATCH_SIZE,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    COLLECTION_NAME,
    CORPUS_LOADER_VERSION,
    DEFAULT_DB_PATH,
    EMBEDDING_MODEL,
    GENERATOR_MODEL,
    PIPELINE_VERSION,
)
from crucible.stubs.rag.chromadb_query import query as chromadb_query
from crucible.stubs.rag.embedder import (
    SentenceTransformersEmbedder,
    get_embedder,
)
from crucible.stubs.rag.exceptions import (
    ChromaDBInitError,
    CollectionNotFoundError,
    EmbeddingError,
)
from crucible.stubs.rag.generator import ClaudeGenerator
from crucible.stubs.rag.ingestion import DocumentIngester
from crucible.stubs.rag.retriever import SemanticRetriever
from crucible.stubs.rag.schema_conformance import validate_rag_output
from crucible.stubs.rag.tracing import PhoenixTracer
from crucible.stubs.rag.zvec_query import query as zvec_query

__all__ = [
    # Chunking
    "FixedChunker",
    "FixedChunkerV2",
    "ConfigurableChunker",
    "ChunkingStrategy",
    # ChromaDB
    "ChromaDBManager",
    "chromadb_query",
    "COLLECTION_NAME",
    "DEFAULT_DB_PATH",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
    "BATCH_SIZE",
    # Constants
    "PIPELINE_VERSION",
    "CORPUS_LOADER_VERSION",
    "EMBEDDING_MODEL",
    "GENERATOR_MODEL",
    # Embedders
    "SentenceTransformersEmbedder",
    "get_embedder",
    # Generator
    "ClaudeGenerator",
    # Exceptions
    "ChromaDBInitError",
    "CollectionNotFoundError",
    "EmbeddingError",
    # Ingestion
    "DocumentIngester",
    # Retrieval
    "SemanticRetriever",
    # Citations
    "extract_citations",
    # Schema validation
    "validate_rag_output",
    # Tracing
    "PhoenixTracer",
    # Zvec
    "zvec_query",
]
