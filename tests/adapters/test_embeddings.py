"""Tests for embeddings module."""

import pytest

from crucible.adapters.embeddings import (
    DEFAULT_HF_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_PROVIDER,
    get_embedder,
    HuggingFaceEmbedder,
    OpenAIEmbedder,
)


def test_huggingface_embedder_initialization():
    """Test HuggingFace embedder initialization."""
    embedder = HuggingFaceEmbedder()
    assert embedder._model_name == DEFAULT_HF_MODEL
    assert embedder._device == "cpu"
    assert embedder._model is None  # Lazy loading


def test_huggingface_embedder_with_custom_device():
    """Test HuggingFace embedder with custom device."""
    embedder = HuggingFaceEmbedder(device="cuda")
    assert embedder._device == "cuda"


def test_huggingface_embedder_empty_texts():
    """Test HuggingFace embedder with empty texts."""
    embedder = HuggingFaceEmbedder()
    result = embedder.embed([])
    assert result == []


def test_openai_embedder_initialization():
    """Test OpenAI embedder initialization."""
    import os

    # Set mock API key for testing
    os.environ["OPENAI_API_KEY"] = "test-key"

    embedder = OpenAIEmbedder()
    assert embedder._model == DEFAULT_OPENAI_MODEL

    del os.environ["OPENAI_API_KEY"]


def test_openai_embedder_empty_texts():
    """Test OpenAI embedder with empty texts."""
    import os

    os.environ["OPENAI_API_KEY"] = "test-key"

    embedder = OpenAIEmbedder()
    result = embedder.embed([])
    assert result == []

    del os.environ["OPENAI_API_KEY"]


def test_get_embedder_default():
    """Test get_embedder with default provider."""
    embedder = get_embedder()
    assert isinstance(embedder, HuggingFaceEmbedder)


def test_get_embedder_huggingface():
    """Test get_embedder with huggingface provider."""
    embedder = get_embedder(provider="huggingface")
    assert isinstance(embedder, HuggingFaceEmbedder)


def test_get_embedder_openai():
    """Test get_embedder with openai provider."""
    import os

    os.environ["OPENAI_API_KEY"] = "test-key"

    embedder = get_embedder(provider="openai")
    assert isinstance(embedder, OpenAIEmbedder)

    del os.environ["OPENAI_API_KEY"]


def test_get_embedder_unsupported_provider():
    """Test get_embedder with unsupported provider."""
    with pytest.raises(ValueError, match="Unsupported embedder provider"):
        get_embedder(provider="unknown")


def test_constants():
    """Test module constants."""
    assert DEFAULT_PROVIDER == "huggingface"
    assert DEFAULT_HF_MODEL == "sentence-transformers/all-MiniLM-L6-v2"
    assert DEFAULT_OPENAI_MODEL == "text-embedding-3-small"
