"""
Custom exceptions for ChromaDB RAG operations.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module defines specific exception types
for different failure modes in the RAG pipeline, following the global error
handling standards.
"""


class ChromaDBInitError(Exception):
    """
    Raised when ChromaDB client initialization fails.

    This exception indicates that the ChromaDB client could not be initialized,
    which may be due to invalid configuration, missing dependencies, or
    permissions issues with the persistent storage directory.
    """


class CollectionNotFoundError(Exception):
    """
    Raised when a requested ChromaDB collection does not exist.

    This exception is raised when attempting to access or modify a collection
    that hasn't been created yet. It can be used to trigger auto-create
    behavior in the ingestion pipeline.
    """


class EmbeddingError(Exception):
    """
    Raised when embedding generation fails.

    This exception indicates that the embedding model failed to generate
    embeddings for the input text, which may be due to model loading errors,
    invalid input, or resource constraints.
    """
