"""Tests for embeddings module."""

import pytest

from crucible.service.deepeval.embeddings import (
    DEFAULT_HF_MODEL,
    DEFAULT_PROVIDER,
    HuggingFaceEmbedder,
    get_embedder,
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


def test_get_embedder_default():
    """Test get_embedder with default provider."""
    embedder = get_embedder()
    assert isinstance(embedder, HuggingFaceEmbedder)


def test_get_embedder_huggingface():
    """Test get_embedder with huggingface provider."""
    embedder = get_embedder(provider="huggingface")
    assert isinstance(embedder, HuggingFaceEmbedder)


def test_get_embedder_openai_removed():
    """OpenAI embedder was removed (Bedrock-only project)."""
    with pytest.raises(ValueError, match="Unsupported embedder provider"):
        get_embedder(provider="openai")


def test_get_embedder_unsupported_provider():
    """Test get_embedder with unsupported provider."""
    with pytest.raises(ValueError, match="Unsupported embedder provider"):
        get_embedder(provider="unknown")


def test_constants():
    """Test module constants."""
    assert DEFAULT_PROVIDER == "huggingface"
    assert DEFAULT_HF_MODEL == "sentence-transformers/all-MiniLM-L6-v2"
