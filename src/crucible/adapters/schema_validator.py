"""
JSON schema validation using JSON Schema Draft 2020-12.

This module provides schema validation for parser output and RAG query output.
All validation uses JSON Schema Draft 2020-12.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError


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


def _load_schema(schema_path: Path) -> dict:
    """
    Load JSON schema from file.

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


def validate(output: dict, schema_path: Path) -> None:
    """
    Validate output against a JSON Schema.

    Uses JSON Schema Draft 2020-12 for validation. Raises SchemaValidationError
    with clear field-level messages when validation fails.

    Args:
        output: Dictionary to validate.
        schema_path: Path to schema file (must use Draft 2020-12).

    Raises:
        SchemaValidationError: If validation fails, with field path and details.
        FileNotFoundError: If schema file doesn't exist.
        ValueError: If schema file contains invalid JSON.

    """
    schema = _load_schema(schema_path)

    # Verify schema is Draft 2020-12
    schema_version = schema.get("$schema", "")
    if "2020-12" not in schema_version:
        raise ValueError(
            f"Schema must use JSON Schema Draft 2020-12, got: {schema_version}"
        )

    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(output))

    if not errors:
        return

    # Report the first error with clear context
    first_error = errors[0]
    field_path = _format_field_path(first_error)

    raise SchemaValidationError(
        message=f"Schema validation failed against {schema_path.name}",
        field_path=field_path,
        original_error=first_error.message,
    )
