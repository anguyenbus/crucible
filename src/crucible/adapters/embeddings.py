"""
Unified embedder factory for RAG and RAGAS.

Provides a single embedder instance that can be shared between:
- RAG retrieval (semantic search)
- RAGAS evaluation (AnswerRelevancy metric)

Supports multiple backends:
- huggingface: Local sentence-transformers (dev/test)
- openai: OpenAI embeddings API
- bedrock: AWS Bedrock Titan embeddings (future)
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Final

from beartype import beartype
from beartype.typing import Protocol

# Constants
DEFAULT_PROVIDER: Final[str] = "huggingface"
DEFAULT_HF_MODEL: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_OPENAI_MODEL: Final[str] = "text-embedding-3-small"


class Embedder(Protocol):
    """Embedder protocol for type safety."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for texts."""
        ...


@beartype
class HuggingFaceEmbedder:
    """Local sentence-transformers embedder for dev/test."""

    __slots__ = ("_model_name", "_model", "_device")

    def __init__(self, model: str = DEFAULT_HF_MODEL, device: str = "cpu") -> None:
        """Initialize HuggingFace embedder."""
        self._model_name: str = model
        self._model: Any = None
        self._device: str = device

    def _load_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name, device=self._device)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for texts."""
        if not texts:
            return []
        model = self._load_model()
        embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]


@beartype
class OpenAIEmbedder:
    """OpenAI embeddings API."""

    __slots__ = ("_client", "_model")

    def __init__(
        self, model: str = DEFAULT_OPENAI_MODEL, api_key: str | None = None
    ) -> None:
        """Initialize OpenAI embedder."""
        import os

        from openai import OpenAI

        self._client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self._model: str = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for texts."""
        if not texts:
            return []

        response = self._client.embeddings.create(input=texts, model=self._model)
        return [item.embedding for item in response.data]


@beartype
class BedrockEmbedder:
    """AWS Bedrock Titan embedder (future)."""

    __slots__ = ("_client", "_model")

    def __init__(
        self,
        model: str = "amazon.titan-embed-text-v2",
        region: str = "us-east-1",
    ) -> None:
        """Initialize Bedrock embedder (not implemented)."""
        raise NotImplementedError(
            "Bedrock embedder not yet implemented. Use huggingface or openai."
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for texts (not implemented)."""
        raise NotImplementedError


@beartype
def get_embedder(
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
    **kwargs: Any,
) -> Embedder:
    """
    Get embedder instance by provider.

    Args:
        provider: Embedder backend - "huggingface", "openai", or "bedrock".
        model: Model name. Defaults vary by provider.
        **kwargs: Additional provider-specific args (device, api_key, etc.)

    Returns:
        Embedder instance conforming to Embedder protocol.

    Raises:
        ValueError: If provider is unsupported.

    """
    if provider == "huggingface":
        return HuggingFaceEmbedder(model=model or DEFAULT_HF_MODEL, **kwargs)
    elif provider == "openai":
        return OpenAIEmbedder(model=model or DEFAULT_OPENAI_MODEL, **kwargs)
    elif provider == "bedrock":
        return BedrockEmbedder(model=model or "amazon.titan-embed-text-v2", **kwargs)
    else:
        raise ValueError(
            f"Unsupported embedder provider: {provider}. "
            "Use huggingface, openai, or bedrock."
        )
