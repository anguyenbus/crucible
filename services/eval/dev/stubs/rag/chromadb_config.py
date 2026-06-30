"""
ChromaDB RAG configuration constants.

NOTE: Reference stub implementation for demonstration purposes.
Not intended for production use. Defines all configuration constants
for the ChromaDB-backed RAG pipeline, including model names and dimensions.
"""

import os
from pathlib import Path
from typing import Final

# ChromaDB storage configuration
CHROMADB_PERSIST_DIR: Final[Path] = Path("data/chromadb/")
DEFAULT_DB_PATH: Final[Path] = CHROMADB_PERSIST_DIR / "chromadb.sqlite3"
COLLECTION_NAME: Final[str] = "legal_rag_bench"

# Embedding model configuration
EMBEDDING_MODEL: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM: Final[int] = 384

# Generator model configuration (set via EVAL_GENERATOR_MODEL env var).
# Bedrock-default: the default is an AU-geographic inference profile.
# Keep the default in sync with generator.DEFAULT_GENERATOR_MODEL.
_DEFAULT_GENERATOR_MODEL: Final[str] = "au.anthropic.claude-sonnet-4-6"


def _resolve_generator_model() -> str:
    """
    Resolve the generator model ID from the EVAL_GENERATOR_MODEL env var.

    Fail-loud rename (O3): if the old RAG_GENERATOR_MODEL is set while
    EVAL_GENERATOR_MODEL is unset, raise — never silently alias the old
    var or fall back to a default.
    """
    if os.getenv("RAG_GENERATOR_MODEL") is not None and (
        os.getenv("EVAL_GENERATOR_MODEL") is None
    ):
        raise ValueError(
            "RAG_GENERATOR_MODEL is renamed to EVAL_GENERATOR_MODEL; update your config."
        )
    return os.getenv("EVAL_GENERATOR_MODEL", _DEFAULT_GENERATOR_MODEL)


GENERATOR_MODEL: Final[str] = _resolve_generator_model()

# Pipeline version tracking
PIPELINE_VERSION: Final[str] = "0.1.0-chromadb"
CORPUS_LOADER_VERSION: Final[str] = "0.1.0"

# Chunking configuration
CHUNK_SIZE: Final[int] = 512
CHUNK_OVERLAP: Final[int] = 0

# Demo showcase specific constants
DEMO_CHUNK_SIZE: Final[int] = 512
DEMO_CHUNK_OVERLAP_BASELINE: Final[int] = 0
DEMO_CHUNK_OVERLAP_CANDIDATE: Final[int] = 150

# Default retrieval configuration
DEFAULT_TOP_K: Final[int] = 5

# Batch processing configuration
BATCH_SIZE: Final[int] = 100
