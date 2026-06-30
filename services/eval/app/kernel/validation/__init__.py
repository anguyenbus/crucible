"""Schema validation primitives (pure kernel)."""

from crucible.kernel.validation.schema_validator import (
    SUPPORTED_SCHEMA_MAJOR,
    SchemaValidationError,
    validate,
)

__all__ = [
    "SUPPORTED_SCHEMA_MAJOR",
    "SchemaValidationError",
    "validate",
]
