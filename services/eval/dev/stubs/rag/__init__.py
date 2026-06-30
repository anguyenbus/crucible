"""RAG stub implementations for the eval dev CLI."""

from dev.stubs.rag.chromadb_client import ChromaDBManager
from dev.stubs.rag.chromadb_config import (
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
from dev.stubs.rag.chromadb_query import query as chromadb_query
from dev.stubs.rag.chunker import FixedChunker
from dev.stubs.rag.chunking import ChunkingStrategy, ConfigurableChunker
from dev.stubs.rag.chunking import FixedChunker as FixedChunkerV2
from dev.stubs.rag.citations import extract_citations
from dev.stubs.rag.embedder import (
    SentenceTransformersEmbedder,
    get_embedder,
)
from dev.stubs.rag.exceptions import (
    ChromaDBInitError,
    CollectionNotFoundError,
    EmbeddingError,
)
from dev.stubs.rag.generator import ClaudeGenerator
from dev.stubs.rag.ingestion import DocumentIngester
from dev.stubs.rag.retriever import SemanticRetriever
from dev.stubs.rag.schema_conformance import validate_rag_output
from dev.stubs.rag.tracing import PhoenixTracer
from dev.stubs.rag.zvec_query import query as zvec_query

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
