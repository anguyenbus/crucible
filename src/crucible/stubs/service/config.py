"""
Stub service boot-time configuration.

This module defines the configuration schema for booting a stub RAG service.
Each stub config describes HOW to bootstrap a service (chunking strategy,
embedding model, port, corpus path, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

# Constants
DEFAULT_EMBEDDING_MODEL: Final[str] = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_PORT: Final[int] = 8080
MIN_CHUNK_SIZE: Final[int] = 1
MAX_CHUNK_SIZE: Final[int] = 8192
MIN_PORT: Final[int] = 1024
MAX_PORT: Final[int] = 65535
DEFAULT_PROJECT_NAME: Final[str] = "case-assistant-synthetic"


class StubConfig(BaseModel):
    """
    Boot-time configuration for stub RAG service.

    This configuration describes HOW to bootstrap a service, including
    chunking strategy, embedding model, port allocation, and corpus location.

    Attributes:
        chunking_strategy: Type of chunking to use (e.g., "fixed").
        chunk_size: Target chunk size in characters. Must be 1-8192.
        chunk_overlap: Overlap between chunks in characters. Must be < chunk_size.
        embedding_model: HuggingFace model identifier for embeddings.
        port: HTTP port for this service instance. Must be unique per service.
        corpus_path: Path to document corpus directory (relative to YAML or absolute).
        phoenix_endpoint: Optional Phoenix endpoint for distributed tracing.
        export_spans: Whether to export OpenInference spans to Phoenix.

    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    chunking_strategy: str = Field(
        ...,
        description="Type of chunking strategy (e.g., 'fixed', 'recursive')"
    )
    chunk_size: int = Field(
        ...,
        ge=MIN_CHUNK_SIZE,
        le=MAX_CHUNK_SIZE,
        description="Target chunk size in characters"
    )
    chunk_overlap: int = Field(
        ...,
        ge=0,
        description="Overlap between chunks in characters"
    )
    embedding_model: str = Field(
        default=DEFAULT_EMBEDDING_MODEL,
        description="HuggingFace model identifier for embeddings"
    )
    port: int = Field(
        ...,
        ge=MIN_PORT,
        le=MAX_PORT,
        description="HTTP port for this service instance"
    )
    corpus_path: Path = Field(
        ...,
        description="Path to document corpus (relative to YAML or absolute)"
    )
    phoenix_endpoint: str | None = Field(
        default="http://localhost:6006",
        description="Phoenix endpoint for distributed tracing"
    )
    export_spans: bool = Field(
        default=True,
        description="Whether to export OpenInference spans to Phoenix"
    )
    retrieval_backend: str = Field(
        default="chromadb",
        description="Retrieval backend: 'chromadb', 'faiss', or 'zvec'"
    )

    @field_validator("chunk_overlap")
    @classmethod
    def validate_overlap_less_than_chunk_size(
        cls, v: int, info: object
    ) -> int:
        """Validate that chunk_overlap is strictly less than chunk_size."""
        if "chunk_size" in info.data and v >= info.data["chunk_size"]:
            raise ValueError(
                f"chunk_overlap ({v}) must be less than chunk_size "
                f"({info.data['chunk_size']})"
            )
        return v

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> Self:
        """
        Load stub configuration from YAML file.

        The corpus_path is resolved relative to the YAML file location
        if it is not already an absolute path.

        Args:
            path: Path to YAML file.

        Returns:
            StubConfig instance.

        Raises:
            FileNotFoundError: If file doesn't exist.
            ValueError: If YAML is invalid or fails validation.

        """
        import yaml

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Stub config not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        # Resolve corpus_path relative to YAML file if not absolute
        corpus_path_str = data.get("corpus_path", "")
        if corpus_path_str:
            corpus_path = Path(corpus_path_str)
            if not corpus_path.is_absolute():
                # Resolve relative to YAML file directory
                yaml_dir = path.parent.resolve()
                corpus_path = yaml_dir / corpus_path
            data["corpus_path"] = corpus_path.resolve()

        return cls(**data)

    @property
    def resolved_corpus_path(self) -> Path:
        """Get the fully resolved corpus path as a Path object."""
        return self.corpus_path.resolve()
