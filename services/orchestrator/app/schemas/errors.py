"""
Error response bodies (Pydantic v2) so 404/422/500/502/503 shapes appear in OpenAPI.

Error mapping for ``POST /query`` (enforced by app-level exception handlers
registered in ``app.main``):
- malformed config ref (not ``{name}-{semver}`` shape) → 422 (validation error)
- unknown-but-well-formed config ref → 404 (no fallback, no ``latest``)
- config-integrity failure (corrupt/tampered packaged artifact) → 500
- upstream dependency failure (OpenSearch failure, non-throttle Bedrock
  ``ClientError``) → 502 with a machine-readable ``dependency`` field
- Bedrock throttling exhausted after bounded retries → 503 with a
  ``Retry-After`` header and ``dependency: "bedrock"``

502/503 bodies NEVER carry raw exception text — eval's runner distinguishes
stack-down from bad-request via the ``dependency`` field, not prose parsing.
"""

from typing import Literal

from pydantic import BaseModel, Field


class NotFoundErrorResponse(BaseModel):
    """404 body: the requested pinned config reference does not exist."""

    detail: str = Field(
        description=(
            "Clear error message naming the unknown {name}-{semver} config "
            "reference. Unknown refs are never resolved by fallback."
        ),
    )


class ValidationErrorItem(BaseModel):
    """One field-level validation error (FastAPI/Pydantic shape)."""

    loc: list[str | int] = Field(description="Path to the failing field.")
    msg: str = Field(description="Human-readable validation message.")
    type: str = Field(description="Machine-readable error type.")


class ValidationErrorResponse(BaseModel):
    """422 body: request validation failed (e.g. malformed pipeline_config ref)."""

    detail: list[ValidationErrorItem] = Field(
        description=(
            "Field-level validation errors (FastAPI shape). Resolver-detected "
            "malformed references emit the SAME list shape via the app-level "
            "handler, so this is the only 422 body variant."
        ),
    )


class InternalErrorResponse(BaseModel):
    """500 body: a packaged config artifact failed integrity verification."""

    detail: str = Field(
        description=(
            "Clear error message naming the config reference whose packaged "
            "artifact is corrupt or mismatches the committed hash manifest. "
            "Internal service — messages are deliberately specific."
        ),
    )


class DependencyErrorResponse(BaseModel):
    """502/503 body: an upstream dependency failed (never a caller error)."""

    detail: str = Field(
        description=(
            "Clean, human-readable failure summary. NEVER raw exception text; "
            "at most the AWS error code or exception class name."
        ),
    )
    dependency: Literal["opensearch", "bedrock"] = Field(
        description=(
            "Machine-readable name of the failing dependency, so callers "
            "(e.g. eval's runner) can distinguish stack-down from bad-request "
            "without parsing prose."
        ),
    )
