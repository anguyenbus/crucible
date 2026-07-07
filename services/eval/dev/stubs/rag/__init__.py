"""
RAG stub implementations for the eval dev CLI.

Exports are resolved lazily (PEP 562) so that importing this package -- or
any single submodule such as ``dev.stubs.rag.bedrock_embedder`` -- does not
require the optional heavy dependencies of OTHER backends (chromadb /
sentence-transformers from the ``demo`` extra, opensearch-py from the
``opensearch`` extra). Each backend's dependencies are imported only when its
symbol is actually accessed.
"""

from importlib import import_module
from typing import Any

# Export name -> (module path, attribute name).
_EXPORTS: dict[str, tuple[str, str]] = {
    # Chunking
    "FixedChunker": ("dev.stubs.rag.chunker", "FixedChunker"),
    "FixedChunkerV2": ("dev.stubs.rag.chunking", "FixedChunker"),
    "ConfigurableChunker": ("dev.stubs.rag.chunking", "ConfigurableChunker"),
    "ChunkingStrategy": ("dev.stubs.rag.chunking", "ChunkingStrategy"),
    # ChromaDB
    "ChromaDBManager": ("dev.stubs.rag.chromadb_client", "ChromaDBManager"),
    "chromadb_query": ("dev.stubs.rag.chromadb_query", "query"),
    "COLLECTION_NAME": ("dev.stubs.rag.chromadb_config", "COLLECTION_NAME"),
    "DEFAULT_DB_PATH": ("dev.stubs.rag.chromadb_config", "DEFAULT_DB_PATH"),
    "CHUNK_SIZE": ("dev.stubs.rag.chromadb_config", "CHUNK_SIZE"),
    "CHUNK_OVERLAP": ("dev.stubs.rag.chromadb_config", "CHUNK_OVERLAP"),
    "BATCH_SIZE": ("dev.stubs.rag.chromadb_config", "BATCH_SIZE"),
    # Constants
    "PIPELINE_VERSION": ("dev.stubs.rag.chromadb_config", "PIPELINE_VERSION"),
    "CORPUS_LOADER_VERSION": ("dev.stubs.rag.chromadb_config", "CORPUS_LOADER_VERSION"),
    "EMBEDDING_MODEL": ("dev.stubs.rag.chromadb_config", "EMBEDDING_MODEL"),
    "GENERATOR_MODEL": ("dev.stubs.rag.chromadb_config", "GENERATOR_MODEL"),
    # Embedders
    "SentenceTransformersEmbedder": ("dev.stubs.rag.embedder", "SentenceTransformersEmbedder"),
    "get_embedder": ("dev.stubs.rag.embedder", "get_embedder"),
    "BedrockTitanEmbedder": ("dev.stubs.rag.bedrock_embedder", "BedrockTitanEmbedder"),
    "get_bedrock_embedder": ("dev.stubs.rag.bedrock_embedder", "get_bedrock_embedder"),
    # Generator
    "ClaudeGenerator": ("dev.stubs.rag.generator", "ClaudeGenerator"),
    # Exceptions
    "ChromaDBInitError": ("dev.stubs.rag.exceptions", "ChromaDBInitError"),
    "CollectionNotFoundError": ("dev.stubs.rag.exceptions", "CollectionNotFoundError"),
    "EmbeddingError": ("dev.stubs.rag.exceptions", "EmbeddingError"),
    # Ingestion
    "DocumentIngester": ("dev.stubs.rag.ingestion", "DocumentIngester"),
    # Retrieval
    "SemanticRetriever": ("dev.stubs.rag.retriever", "SemanticRetriever"),
    "opensearch_query": ("dev.stubs.rag.opensearch_query", "query"),
    # Citations
    "extract_citations": ("dev.stubs.rag.citations", "extract_citations"),
    # Schema validation
    "validate_rag_output": ("dev.stubs.rag.schema_conformance", "validate_rag_output"),
    # Tracing
    "PhoenixTracer": ("dev.stubs.rag.tracing", "PhoenixTracer"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve exports lazily on first access (PEP 562)."""
    try:
        module_path, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(import_module(module_path), attribute)


def __dir__() -> list[str]:
    """Expose lazy exports to dir()/completion."""
    return __all__
