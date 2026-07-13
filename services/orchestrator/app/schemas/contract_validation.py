"""
Packaged-schema loading and JSON Schema Draft 2020-12 validation.

Follows eval's ``app/kernel/validation/schema_validator.py`` approach: schemas
are resolved via ``importlib.resources`` against the packaged ``app.contracts``
— there is NO CWD-relative ``Path(...)`` anywhere in this module, so it works
identically from the source tree and an installed wheel.

The ``/query`` envelope's ``result`` block is validated against
``rag_query_output.schema.json`` VERBATIM — no key stripping anywhere
(strip-before-validate would neuter the schema's ``additionalProperties:
false`` drift detection).
"""

from __future__ import annotations

import functools
import json
from importlib import resources
from typing import Any, Final

from jsonschema import Draft202012Validator, ValidationError

# Packaged schemas live in the in-package mirror that ships in the wheel
# (eval's copy is canonical; app/contracts/ holds the byte-for-byte mirror).
_CONTRACTS_PACKAGE: Final[str] = "app.contracts"


class ResultValidationError(Exception):
    """
    Raised when a payload fails schema validation.

    The message includes the failing field path and the original jsonschema
    error details.
    """

    def __init__(self, message: str, field_path: str = "", original_error: str = ""):
        """
        Initialize the validation error.

        Args:
            message: Human-readable error message.
            field_path: JSON path to the failing field (e.g. "$.answer.text").
            original_error: Original jsonschema validation error message.

        """
        self.field_path = field_path
        self.original_error = original_error
        full_message = message
        if field_path:
            full_message += f" (field: {field_path})"
        if original_error:
            full_message += f" | {original_error}"
        super().__init__(full_message)


def load_packaged_schema(schema: str = "rag_query_output") -> dict[str, Any]:
    """
    Load a packaged JSON schema by logical name via ``importlib.resources``.

    Resolves ``<schema>.schema.json`` inside the packaged ``app.contracts``
    (no CWD-relative paths; run-from-anywhere).

    Args:
        schema: Logical schema name (e.g. ``"rag_query_output"``).

    Returns:
        Parsed schema dictionary.

    Raises:
        FileNotFoundError: If the named schema resource is not packaged.
        ValueError: If the packaged schema contains invalid JSON or does not
            declare JSON Schema Draft 2020-12.

    """
    resource_name = f"{schema}.schema.json"
    resource = resources.files(_CONTRACTS_PACKAGE).joinpath(resource_name)
    if not resource.is_file():
        raise FileNotFoundError(
            f"Packaged schema not found: {resource_name} in {_CONTRACTS_PACKAGE}"
        )

    try:
        loaded = json.loads(resource.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in packaged schema {resource_name}: {e}") from e

    dialect = loaded.get("$schema", "")
    if "2020-12" not in dialect:
        raise ValueError(f"Schema must use JSON Schema Draft 2020-12, got: {dialect}")

    return loaded


@functools.lru_cache
def _validator(schema: str) -> Draft202012Validator:
    """
    Build (once) and cache the compiled Draft 2020-12 validator for a schema.

    Schemas are packaged, immutable artifacts, so compiling the validator per
    call is pure waste; ``lru_cache`` never caches exceptions, so a broken
    schema is re-checked on every call.
    """
    return Draft202012Validator(load_packaged_schema(schema))


def validate_result(payload: dict[str, Any], *, schema: str = "rag_query_output") -> None:
    """
    Validate ``payload`` VERBATIM against a packaged schema (Draft 2020-12).

    Args:
        payload: The dictionary to validate, passed through untouched — callers
            must never strip or inject keys before validation.
        schema: Logical schema name resolved against the packaged
            ``app.contracts``.

    Raises:
        ResultValidationError: If validation fails; carries a clear field path.

    """
    errors: list[ValidationError] = list(_validator(schema).iter_errors(payload))
    if not errors:
        return

    first_error = errors[0]
    raise ResultValidationError(
        message=f"Schema validation failed against {schema}.schema.json",
        field_path=first_error.json_path,
        original_error=first_error.message,
    )
