"""
Embedder for RAG operations.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use.
"""

from __future__ import annotations

from typing import Any

from beartype import beartype


@beartype
class SentenceTransformersEmbedder:
    """
    SentenceTransformers embedder for RAG operations.

    NOTE: This is a reference stub implementation for demonstration purposes.
    It is not intended for production use.

    Attributes:
        _model: The SentenceTransformer model instance.
        _model_name: Name of the model being used.

    """

    __slots__ = ("_model", "_model_name")

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        """
        Initialize the embedder.

        Args:
            model_name: HuggingFace model name. Default: all-MiniLM-L6-v2.

        """
        self._model_name: str = model_name
        self._model: Any = None

    def _load_model(self) -> Any:
        """Load the SentenceTransformer model lazily."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name, device="cpu")
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for texts.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors (list of floats).

        """
        if not texts:
            return []

        model = self._load_model()
        embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]


@beartype
def get_embedder(
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
) -> SentenceTransformersEmbedder:
    """
    Get a SentenceTransformers embedder instance.

    Args:
        model_name: HuggingFace model name.

    Returns:
        SentenceTransformersEmbedder instance.

    """
    return SentenceTransformersEmbedder(model_name=model_name)
