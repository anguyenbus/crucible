"""
ChromaDB client manager for vector storage operations.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the ChromaDBManager class
which handles all ChromaDB operations including collection management, connection
lifecycle, and embedding function configuration.
"""

from __future__ import annotations

import chromadb
from chromadb.utils import embedding_functions
from rich.console import Console

from crucible.local.stubs.rag.chromadb_config import CHROMADB_PERSIST_DIR, EMBEDDING_MODEL
from crucible.local.stubs.rag.exceptions import ChromaDBInitError, CollectionNotFoundError

console = Console()


class ChromaDBManager:
    """
    Manager for ChromaDB client and collection operations.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    The ChromaDBManager encapsulates all ChromaDB operations including client
    initialization, collection management (get/create/delete), and connection
    lifecycle. It supports both persistent storage mode and in-memory mode
    for testing.

    Attributes:
        _client: ChromaDB client instance (persistent or in-memory).
        _persist: Whether to use persistent storage (True) or in-memory (False).

    Example:
        >>> manager = ChromaDBManager(persist=True)
        >>> collection = manager.get_or_create_collection("my_collection")
        >>> manager.delete_collection("my_collection")
        >>> manager.close()

    """

    __slots__ = ("_client", "_persist")

    def __init__(self, persist: bool = True) -> None:
        """
        Initialize ChromaDB client manager.

        Args:
            persist: If True, use persistent storage at CHROMADB_PERSIST_DIR.
                If False, use in-memory storage for testing. Default: True.

        Raises:
            ChromaDBInitError: If ChromaDB client initialization fails.

        """
        self._persist: bool = persist

        try:
            if persist:
                # Ensure persist directory exists
                CHROMADB_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
                # Use persistent mode with SQLite backend
                self._client = chromadb.PersistentClient(path=str(CHROMADB_PERSIST_DIR))
            else:
                # Use in-memory mode for testing
                self._client = chromadb.Client()

        except Exception as e:
            raise ChromaDBInitError(
                f"Failed to initialize ChromaDB client in "
                f"{'persistent' if persist else 'in-memory'} mode: {e}"
            ) from e

    def get_or_create_collection(
        self,
        name: str,
    ) -> chromadb.Collection:
        """
        Get existing collection or create if it doesn't exist.

        This method uses the SentenceTransformers embedding function configured
        with EMBEDDING_MODEL (all-MiniLM-L6-v2) which produces 384-dim vectors.

        Note: ChromaDB manages its own embedder for collection operations.
        This is separate from the shared embedder used for RAG retrieval/RAGAS.

        Args:
            name: Collection name identifier.

        Returns:
            ChromaDB collection instance with SentenceTransformers embedding function.

        Raises:
            ChromaDBInitError: If collection creation fails.

        """
        try:
            # Configure SentenceTransformers embedding function
            embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL,
                device="cpu",
            )

            return self._client.get_or_create_collection(
                name=name,
                embedding_function=embedding_function,
            )

        except Exception as e:
            raise ChromaDBInitError(f"Failed to get or create collection '{name}': {e}") from e

    def collection_exists(self, name: str) -> bool:
        """
        Check if a collection exists in the ChromaDB database.

        Args:
            name: Collection name to check.

        Returns:
            True if collection exists, False otherwise.

        """
        try:
            # List collections and check for name match
            collections = self._client.list_collections()
            return any(collection.name == name for collection in collections)

        except Exception:
            # On error, conservatively return False
            return False

    def delete_collection(self, name: str) -> None:
        """
        Delete a collection by name.

        Args:
            name: Collection name to delete.

        Raises:
            CollectionNotFoundError: If collection doesn't exist.
            ChromaDBInitError: If deletion fails for other reasons.

        """
        if not self.collection_exists(name):
            raise CollectionNotFoundError(f"Collection '{name}' does not exist")

        try:
            self._client.delete_collection(name=name)

        except Exception as e:
            raise ChromaDBInitError(f"Failed to delete collection '{name}': {e}") from e

    def close(self) -> None:
        """
        Close ChromaDB client connection.

        For persistent mode, this ensures proper cleanup. For in-memory mode,
        this is a no-op but provides a consistent interface.
        """
        # ChromaDB client doesn't require explicit closing,
        # but we provide this method for interface consistency
        # and future compatibility
        self._client = None
