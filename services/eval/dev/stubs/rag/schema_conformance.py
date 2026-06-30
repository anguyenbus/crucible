"""
Schema validation for RAG query output.

NOTE: This is a reference stub implementation provided for demonstration purposes.
It is not intended for production use. This module provides the validate_rag_output
function which validates RAG output against the rag_query_output schema.
"""

from __future__ import annotations

from typing import Any

from app.kernel.validation.schema_validator import SchemaValidationError
from app.kernel.validation.schema_validator import (
    validate as schema_validate,
)


def validate_rag_output(output: dict[str, Any]) -> None:
    """
    Validate RAG query output against rag_query_output.schema.json.

    This function ensures that the RAG output conforms to the expected
    schema structure, including all required fields and version constants.

    Args:
        output: RAG query output dictionary to validate.

    Raises:
        SchemaValidationError: If output fails schema validation.
        FileNotFoundError: If schema file doesn't exist.
        ValueError: If schema file contains invalid JSON or wrong schema version.

    """
    # Resolve the packaged schema by logical name (run-from-anywhere; no
    # CWD-relative path). The kernel validator anchors on app.contracts
    # via importlib.resources, so this works in the source tree and an installed
    # wheel alike (Finding A).
    schema_validate(output, schema="rag_query_output")

    # Additional validation for version constants
    _validate_version_constants(output)

    # Additional validation for citations
    _validate_citations(output)


def _validate_version_constants(output: dict[str, Any]) -> None:
    """
    Validate that version constants match expected values.

    Args:
        output: RAG query output dictionary.

    Raises:
        SchemaValidationError: If version constants don't match.

    """
    from dev.stubs.rag.chromadb_config import (
        CORPUS_LOADER_VERSION,
        EMBEDDING_MODEL,
        GENERATOR_MODEL,
        PIPELINE_VERSION,
    )

    system_version = output.get("system_version", {})

    # Validate pipeline_version
    pipeline_version = system_version.get("pipeline_version", "")
    if pipeline_version != PIPELINE_VERSION:
        raise SchemaValidationError(
            f"pipeline_version mismatch: expected '{PIPELINE_VERSION}', got '{pipeline_version}'",
            field_path="system_version.pipeline_version",
        )

    # Validate corpus_loader_version
    corpus_version = system_version.get("corpus_loader_version", "")
    if corpus_version != CORPUS_LOADER_VERSION:
        raise SchemaValidationError(
            f"corpus_loader_version mismatch: expected '{CORPUS_LOADER_VERSION}', "
            f"got '{corpus_version}'",
            field_path="system_version.corpus_loader_version",
        )

    # Validate embedder_model
    embedder_model = system_version.get("embedder_model", "")
    if embedder_model != EMBEDDING_MODEL:
        raise SchemaValidationError(
            f"embedder_model mismatch: expected '{EMBEDDING_MODEL}', got '{embedder_model}'",
            field_path="system_version.embedder_model",
        )

    # Validate generator_model
    generator_model = system_version.get("generator_model", "")
    if generator_model != GENERATOR_MODEL:
        raise SchemaValidationError(
            f"generator_model mismatch: expected '{GENERATOR_MODEL}', got '{generator_model}'",
            field_path="system_version.generator_model",
        )


def _validate_citations(output: dict[str, Any]) -> None:
    """
    Validate that citations reference valid chunk_ids.

    Args:
        output: RAG query output dictionary.

    Raises:
        SchemaValidationError: If citations reference invalid chunk_ids.

    """
    retrieved_chunks = output.get("retrieved_chunks", [])
    valid_chunk_ids = {chunk.get("chunk_id", "") for chunk in retrieved_chunks}

    answer = output.get("answer", {})
    citations = answer.get("citations", [])

    for citation in citations:
        chunk_ids = citation.get("chunk_ids", [])
        for chunk_id in chunk_ids:
            if chunk_id not in valid_chunk_ids:
                raise SchemaValidationError(
                    f"Citation references invalid chunk_id: '{chunk_id}'",
                    field_path="answer.citations",
                )

    # Validate all retrieved_chunks have char_span
    for chunk in retrieved_chunks:
        if "char_span" not in chunk:
            raise SchemaValidationError(
                "Retrieved chunk missing char_span",
                field_path="retrieved_chunks",
            )
