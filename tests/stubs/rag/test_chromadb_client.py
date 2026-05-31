"""Tests for ChromaDB client manager."""

import tempfile
from pathlib import Path

import pytest

from crucible.stubs.rag.chromadb_client import ChromaDBManager
from crucible.stubs.rag.exceptions import ChromaDBInitError, CollectionNotFoundError


def test_chromadb_manager_initialization_persistent(tmp_path: Path):
    """Test ChromaDB manager initialization with persistent storage."""
    # Mock the persist directory
    from crucible.stubs.rag import chromadb_config
    original_dir = chromadb_config.CHROMADB_PERSIST_DIR
    chromadb_config.CHROMADB_PERSIST_DIR = tmp_path / "chromadb"

    try:
        manager = ChromaDBManager(persist=True)
        assert manager._persist is True
        assert manager._client is not None
        manager.close()
    finally:
        chromadb_config.CHROMADB_PERSIST_DIR = original_dir


def test_chromadb_manager_initialization_in_memory():
    """Test ChromaDB manager initialization with in-memory storage."""
    manager = ChromaDBManager(persist=False)
    assert manager._persist is False
    assert manager._client is not None
    manager.close()


def test_chromadb_get_or_create_collection(tmp_path: Path):
    """Test creating a collection."""
    from crucible.stubs.rag import chromadb_config
    original_dir = chromadb_config.CHROMADB_PERSIST_DIR
    chromadb_config.CHROMADB_PERSIST_DIR = tmp_path / "chromadb"

    try:
        manager = ChromaDBManager(persist=True)
        collection = manager.get_or_create_collection("test_collection")

        assert collection is not None
        assert collection.name == "test_collection"
        manager.close()
    finally:
        chromadb_config.CHROMADB_PERSIST_DIR = original_dir


def test_chromadb_collection_exists(tmp_path: Path):
    """Test checking if collection exists."""
    from crucible.stubs.rag import chromadb_config
    original_dir = chromadb_config.CHROMADB_PERSIST_DIR
    chromadb_config.CHROMADB_PERSIST_DIR = tmp_path / "chromadb"

    try:
        manager = ChromaDBManager(persist=True)

        # Collection doesn't exist initially
        assert manager.collection_exists("test_collection") is False

        # Create collection
        manager.get_or_create_collection("test_collection")

        # Now it exists
        assert manager.collection_exists("test_collection") is True

        manager.close()
    finally:
        chromadb_config.CHROMADB_PERSIST_DIR = original_dir


def test_chromadb_delete_collection(tmp_path: Path):
    """Test deleting a collection."""
    from crucible.stubs.rag import chromadb_config
    original_dir = chromadb_config.CHROMADB_PERSIST_DIR
    chromadb_config.CHROMADB_PERSIST_DIR = tmp_path / "chromadb"

    try:
        manager = ChromaDBManager(persist=True)

        # Create collection
        manager.get_or_create_collection("test_collection")
        assert manager.collection_exists("test_collection") is True

        # Delete collection
        manager.delete_collection("test_collection")
        assert manager.collection_exists("test_collection") is False

        manager.close()
    finally:
        chromadb_config.CHROMADB_PERSIST_DIR = original_dir


def test_chromadb_delete_nonexistent_collection(tmp_path: Path):
    """Test deleting a non-existent collection raises error."""
    from crucible.stubs.rag import chromadb_config
    original_dir = chromadb_config.CHROMADB_PERSIST_DIR
    chromadb_config.CHROMADB_PERSIST_DIR = tmp_path / "chromadb"

    try:
        manager = ChromaDBManager(persist=True)

        with pytest.raises(CollectionNotFoundError, match="does not exist"):
            manager.delete_collection("nonexistent_collection")

        manager.close()
    finally:
        chromadb_config.CHROMADB_PERSIST_DIR = original_dir


def test_chromadb_constants():
    """Test ChromaDB configuration constants."""
    from crucible.stubs.rag.chromadb_config import (
        BATCH_SIZE,
        CHUNK_OVERLAP,
        CHUNK_SIZE,
        DEFAULT_TOP_K,
        EMBEDDING_DIM,
        EMBEDDING_MODEL,
        GENERATOR_MODEL,
        PIPELINE_VERSION,
    )

    assert EMBEDDING_MODEL == "sentence-transformers/all-MiniLM-L6-v2"
    assert EMBEDDING_DIM == 384
    assert PIPELINE_VERSION == "0.1.0-chromadb"
    assert CHUNK_SIZE == 512
    assert CHUNK_OVERLAP == 0
    assert DEFAULT_TOP_K == 5
    assert BATCH_SIZE == 100
