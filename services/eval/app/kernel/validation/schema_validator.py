"""
JSON schema validation using JSON Schema Draft 2020-12.

This module provides schema validation for parser output and RAG query output.
All validation uses JSON Schema Draft 2020-12.

It is part of the pure ``app.kernel`` and resolves packaged schemas via
``importlib.resources`` against ``app.contracts`` -- there is NO
CWD-relative ``Path(...)`` anywhere in this module. Callers pass either a logical
schema name (``schema="rag_query_output"``) or an explicit ``schema_path`` (which
WINS, for tests/local callers).
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Final

from jsonschema import Draft202012Validator, ValidationError

# Packaged schemas live in the in-package vendored copy that ships in the wheel.
# (Top-level contracts/ single-sourcing is Phase 5.)
_CONTRACTS_PACKAGE: Final[str] = "app.contracts"

# Major schema version this kernel understands. The loaded schema's
# ``schema_version`` const is parsed and its major compared against this; an
# unsupported major raises SchemaValidationError (the unknown-major-version gate).
SUPPORTED_SCHEMA_MAJOR: Final[int] = 1


class SchemaValidationError(Exception):
    """
    Raised when output fails schema validation.

    The error message includes the field path and validation error details.
    """

    def __init__(self, message: str, field_path: str = "", original_error: str = ""):
        """
        Initialize schema validation error.

        Args:
            message: Human-readable error message.
            field_path: JSON path to the failing field (e.g., "elements[0].bbox").
            original_error: Original validation error message.

        """
        self.field_path = field_path
        self.original_error = original_error
        full_message = f"{message}"
        if field_path:
            full_message += f" (field: {field_path})"
        if original_error:
            full_message += f" | {original_error}"
        super().__init__(full_message)


def _load_schema_from_path(schema_path: Path) -> dict:
    """
    Load a JSON schema from an explicit filesystem path.

    Args:
        schema_path: Path to schema file.

    Returns:
        Parsed schema dictionary.

    Raises:
        FileNotFoundError: If schema file doesn't exist.
        ValueError: If schema file contains invalid JSON.

    """
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    try:
        with open(schema_path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in schema file {schema_path}: {e}") from e


def _load_schema_by_name(schema: str) -> dict:
    """
    Load a packaged schema by logical name via importlib.resources.

    Resolves ``<schema>.schema.json`` inside the packaged ``app.contracts``
    (no CWD-relative paths). This is the run-from-anywhere path (Finding A fix).

    Args:
        schema: Logical schema name (e.g. "rag_query_output").

    Returns:
        Parsed schema dictionary.

    Raises:
        FileNotFoundError: If the named schema resource is not packaged.
        ValueError: If the packaged schema contains invalid JSON.

    """
    resource_name = f"{schema}.schema.json"
    resource = resources.files(_CONTRACTS_PACKAGE).joinpath(resource_name)
    if not resource.is_file():
        raise FileNotFoundError(
            f"Packaged schema not found: {resource_name} in {_CONTRACTS_PACKAGE}"
        )

    try:
        return json.loads(resource.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in packaged schema {resource_name}: {e}") from e


def _format_field_path(error: ValidationError) -> str:
    """
    Format JSON path from validation error.

    Args:
        error: JSON Schema ValidationError.

    Returns:
        Dot-notation path string (e.g., "elements[0].bbox").

    """
    path = error.json_path
    if not path:
        return ""

    parts = []
    for part in path:
        if isinstance(part, int):
            # Array index
            parts.append(f"[{part}]")
        elif parts and isinstance(parts[-1], str) and not parts[-1].endswith("]"):
            # Object key - add dot separator
            parts.append(f".{part}")
        else:
            # First part or after array index
            parts.append(str(part))

    return "".join(parts)


def _check_supported_major(schema: dict, label: str) -> None:
    """
    Gate the loaded schema by its declared major version.

    Reads the schema's ``schema_version`` const (a JSON Schema ``const`` under
    ``properties.schema_version``), parses its major, and raises if the major is
    not the one this kernel supports.

    Args:
        schema: Loaded schema dictionary.
        label: Human-readable schema label for the error message.

    Raises:
        SchemaValidationError: If the schema declares an unsupported major version.

    """
    version_spec = schema.get("properties", {}).get("schema_version", {})
    declared = version_spec.get("const") if isinstance(version_spec, dict) else None
    if declared is None:
        # No declared schema_version const -> nothing to gate (older/looser schemas).
        return

    try:
        major = int(str(declared).split(".", 1)[0])
    except (ValueError, AttributeError) as e:
        raise SchemaValidationError(
            message=f"Schema {label} declares an unparseable schema_version: {declared!r}",
            original_error=str(e),
        ) from e

    if major != SUPPORTED_SCHEMA_MAJOR:
        raise SchemaValidationError(
            message=(
                f"Schema {label} declares schema_version major {major} "
                f"(const {declared!r}); this kernel supports major "
                f"{SUPPORTED_SCHEMA_MAJOR} only."
            ),
        )


def validate(
    output: dict,
    *,
    schema: str | None = None,
    schema_path: Path | None = None,
) -> None:
    """
    Validate output against a JSON Schema (Draft 2020-12).

    Resolution order:
    - An explicit ``schema_path`` WINS (injectable, for tests/local callers).
    - Otherwise ``schema`` is a logical name resolved via ``importlib.resources``
      against the packaged ``app.contracts`` (run-from-anywhere; no CWD path).

    The loaded schema must declare ``$schema`` Draft 2020-12, and its
    ``schema_version`` const (if present) must have a supported major version.

    Args:
        output: Dictionary to validate.
        schema: Logical schema name (e.g. "rag_query_output"). Used when
            ``schema_path`` is not given.
        schema_path: Explicit path to a schema file. Wins over ``schema``.

    Raises:
        SchemaValidationError: If validation fails (incl. unsupported major).
        ValueError: If neither argument is given, or the schema is not Draft
            2020-12, or the schema JSON is invalid.
        FileNotFoundError: If the resolved schema cannot be found.

    """
    if schema_path is not None:
        loaded = _load_schema_from_path(schema_path)
        label = schema_path.name
    elif schema is not None:
        loaded = _load_schema_by_name(schema)
        label = f"{schema}.schema.json"
    else:
        raise ValueError("validate() requires either `schema` (logical name) or `schema_path`.")

    # Verify schema is Draft 2020-12.
    schema_version = loaded.get("$schema", "")
    if "2020-12" not in schema_version:
        raise ValueError(f"Schema must use JSON Schema Draft 2020-12, got: {schema_version}")

    # Minimal major-version gating (unknown-major-version raises).
    _check_supported_major(loaded, label)

    validator = Draft202012Validator(loaded)
    errors = list(validator.iter_errors(output))

    if not errors:
        return

    # Report the first error with clear context.
    first_error = errors[0]
    field_path = _format_field_path(first_error)

    raise SchemaValidationError(
        message=f"Schema validation failed against {label}",
        field_path=field_path,
        original_error=first_error.message,
    )
