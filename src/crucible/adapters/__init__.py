"""Adapters for crucible."""

from crucible.adapters.deepeval_adapter import (
    DEFAULT_JUDGE_MODEL,
    DEFAULT_LLM_PROVIDER,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_TEMPERATURE,
    DeepEvalEvaluator,
    transform_to_deepeval_sample,
)
from crucible.adapters.embeddings import (
    DEFAULT_HF_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_PROVIDER,
    BedrockEmbedder,
    Embedder,
    HuggingFaceEmbedder,
    OpenAIEmbedder,
    get_embedder,
)
from crucible.adapters.rag_adapter import RagAdapter
from crucible.adapters.schema_validator import SchemaValidationError, validate

__all__ = [
    # deepeval_adapter
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_LLM_PROVIDER",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_TEMPERATURE",
    "DeepEvalEvaluator",
    "transform_to_deepeval_sample",
    # embeddings
    "DEFAULT_HF_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_PROVIDER",
    "BedrockEmbedder",
    "Embedder",
    "HuggingFaceEmbedder",
    "OpenAIEmbedder",
    "get_embedder",
    # rag_adapter
    "RagAdapter",
    # schema_validator
    "SchemaValidationError",
    "validate",
]
