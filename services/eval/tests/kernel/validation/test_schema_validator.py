"""Test-first (RED) coverage for the kernel schema validator.

Targets the FUTURE kernel path ``app.kernel.validation.schema_validator``
(Commit 4 moves the module there with the new signature). Until then these tests
fail at import/collection time.

New signature under test:
    validate(output, *, schema: str | None = None, schema_path: Path | None = None) -> None

- ``schema`` is a logical name resolved via importlib.resources against the
  packaged ``app.contracts`` (no CWD-relative paths).
- explicit ``schema_path`` WINS and is injectable for tests/local callers.
- a ``SUPPORTED_SCHEMA_MAJOR`` constant gates the loaded schema's
  ``schema_version`` const; an unsupported major raises SchemaValidationError.

Deterministic fixtures only (NO hypothesis).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kernel.validation.schema_validator import (
    SUPPORTED_SCHEMA_MAJOR,
    SchemaValidationError,
    validate,
)

# A minimal valid rag_query_output payload (schema_version "1.0.0", all required
# keys present). The packaged rag_query_output schema requires schema_version,
# system_version (object with pipeline_version), query (query_id + text), answer
# (text + citations), and retrieved_chunks (array).
_VALID_RAG_OUTPUT = {
    "schema_version": "1.0.0",
    "system_version": {"pipeline_version": "1.0.0"},
    "query": {"query_id": "q1", "text": "What is the termination clause?"},
    "answer": {"text": "The contract may be terminated with notice.", "citations": []},
    "retrieved_chunks": [],
}


def test_valid_output_passes_via_logical_name() -> None:
    """A conformant payload validates against the logical 'rag_query_output' schema."""
    # Returns None on success (no raise).
    assert validate(_VALID_RAG_OUTPUT, schema="rag_query_output") is None


def test_invalid_output_raises_schema_validation_error() -> None:
    """A payload that violates the schema raises SchemaValidationError."""
    invalid = dict(_VALID_RAG_OUTPUT)
    # retrieved_chunks must be an array; a string violates the type.
    invalid["retrieved_chunks"] = "not-a-list"

    with pytest.raises(SchemaValidationError):
        validate(invalid, schema="rag_query_output")


def test_missing_required_field_raises_with_field_path() -> None:
    """A missing required field raises SchemaValidationError naming the field."""
    missing = dict(_VALID_RAG_OUTPUT)
    del missing["answer"]

    with pytest.raises(SchemaValidationError) as exc_info:
        validate(missing, schema="rag_query_output")

    # The error surfaces the offending field name somewhere in its message.
    assert "answer" in str(exc_info.value)


def test_unknown_major_version_raises(tmp_path: Path) -> None:
    """A schema whose schema_version major is unsupported raises (gating)."""
    unsupported_major = SUPPORTED_SCHEMA_MAJOR + 1
    bogus_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "schema_version": {
                "type": "string",
                "const": f"{unsupported_major}.0.0",
            },
        },
    }
    schema_file = tmp_path / "bogus.schema.json"
    schema_file.write_text(json.dumps(bogus_schema))

    with pytest.raises(SchemaValidationError):
        # Explicit schema_path wins (injectable); the major-version gate fires.
        validate(_VALID_RAG_OUTPUT, schema_path=schema_file)


def test_explicit_schema_path_wins_over_logical_name(tmp_path: Path) -> None:
    """When both are given, the injected schema_path is used (not the name)."""
    permissive = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": "1.0.0"},
        },
    }
    schema_file = tmp_path / "permissive.schema.json"
    schema_file.write_text(json.dumps(permissive))

    # This payload would FAIL the strict rag_query_output schema, but the
    # injected permissive schema_path accepts it -> schema_path wins.
    minimal = {"schema_version": "1.0.0"}
    assert validate(minimal, schema="rag_query_output", schema_path=schema_file) is None
