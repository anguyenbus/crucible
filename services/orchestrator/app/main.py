"""
FastAPI entry point for the orchestrator service.

Phase 2 (walking skeleton): ``POST /query`` runs the REAL pipeline —
OpenSearch hybrid retrieval → context assembly → prompt build → Bedrock
generation → citation build — with clients constructed ONCE in lifespan and
stored on ``app.state``; ``GET /healthz`` (liveness) and ``GET /readyz``
(real dependency readiness) are the health probes. Importing this module
performs NO environment reads and NO I/O — Settings, clients, and the tracer
all resolve at lifespan.

Tests never reach AWS: lifespan constructs clients/settings/tracer ONLY when
none are pre-installed on ``app.state``, so mocked instances substituted
before startup win.

ALL error mapping lives HERE, at app level, via ``add_exception_handler`` —
routes raise domain exceptions and never carry per-route try/except:
- config resolution: 422 / 404 / 500 (Phase 1, unchanged)
- ``BedrockThrottleExhaustedError`` → 503 + ``Retry-After``
  (transient; ``dependency: "bedrock"``)
- non-throttle Bedrock ``ClientError`` → 502 (``dependency: "bedrock"``)
- OpenSearch failures (transport errors, not-ready client) → 502
  (``dependency: "opensearch"``)
502/503 bodies carry the machine-readable ``dependency`` field and NEVER raw
exception text.

Run locally with: ``uv run uvicorn app.main:app``.
"""

from contextlib import asynccontextmanager
from typing import Final

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opensearchpy.exceptions import OpenSearchException

from app.clients import build_app_clients
from app.clients.errors import BedrockThrottleExhaustedError, OpenSearchNotReadyError
from app.config import (
    ConfigIntegrityError,
    MalformedConfigRefError,
    UnknownConfigError,
    get_settings,
)
from app.observability import configure_tracer
from app.routers.analyze import analyze_router
from app.routers.compare import compare_router
from app.routers.health import health_router
from app.routers.query import query_router

# Retry-After (seconds) on 503: past the client's own capped backoff budget,
# so an immediate re-request does not just re-exhaust the retries.
_RETRY_AFTER_SECONDS: Final[int] = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Construct Settings, the tracer, and the AWS clients ONCE per process.

    Each slot is filled only when absent so tests can pre-install mocks on
    ``app.state`` (the substitution seam); a pre-set slot is never rebuilt or
    torn down here.
    """
    if getattr(app.state, "settings", None) is None:
        app.state.settings = get_settings()
    if getattr(app.state, "tracer", None) is None:
        app.state.tracer, app.state.tracer_provider = configure_tracer(
            app.state.settings.phoenix_endpoint
        )
    if getattr(app.state, "clients", None) is None:
        app.state.clients = build_app_clients(app.state.settings)
    yield
    provider = getattr(app.state, "tracer_provider", None)
    if provider is not None:
        provider.shutdown()


app = FastAPI(
    title="orchestrator",
    version="0.1.0",
    description=(
        "RAG query-path orchestrator. Phase 2 (walking skeleton): /query runs "
        "the real OpenSearch retrieval + Bedrock generation pipeline, marked "
        "generation_mode='live', with per-stage OpenInference spans exported "
        "to Phoenix when PHOENIX_ENDPOINT is set."
    ),
    lifespan=lifespan,
)


def _handle_unknown_config(request: Request, exc: Exception) -> JSONResponse:
    """Unknown-but-well-formed config ref → 404 (NotFoundErrorResponse shape)."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def _handle_malformed_config_ref(request: Request, exc: Exception) -> JSONResponse:
    """
    Malformed config ref → 422 in FastAPI's field-level list shape.

    Defense in depth: QueryRequest's pattern normally rejects malformed refs
    first (producing this exact shape via Pydantic), so the resolver-raised
    path emits the SAME documented ``ValidationErrorResponse`` list shape —
    the contract has a single truthful 422 body variant.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "loc": ["body", "pipeline_config"],
                    "msg": str(exc),
                    "type": "string_pattern_mismatch",
                }
            ]
        },
    )


def _handle_config_integrity(request: Request, exc: Exception) -> JSONResponse:
    """
    Corrupt/tampered packaged config artifact → 500 with a clean JSON body.

    The message names the failing config artifact (this is an internal
    service; specific messages beat opaque ones).
    """
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def _handle_bedrock_throttle_exhausted(request: Request, exc: Exception) -> JSONResponse:
    """
    Bedrock throttling outlasted the bounded retries → 503 + Retry-After.

    Transient by definition (DependencyErrorResponse shape, dependency:
    bedrock); the message is the client's own clean prose, never a raw
    botocore error.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc), "dependency": "bedrock"},
        headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
    )


def _handle_bedrock_client_error(request: Request, exc: Exception) -> JSONResponse:
    """
    Non-throttle Bedrock ``ClientError`` → 502 naming the dependency.

    Only the AWS error CODE is echoed (machine-usable, e.g.
    AccessDeniedException) — the raw error message never leaks.
    """
    response = getattr(exc, "response", None)
    code = "unknown"
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "unknown")
    return JSONResponse(
        status_code=502,
        content={
            "detail": f"Bedrock request failed (AWS error code: {code}).",
            "dependency": "bedrock",
        },
    )


def _handle_opensearch_failure(request: Request, exc: Exception) -> JSONResponse:
    """
    OpenSearch transport/search failure → 502 naming the dependency.

    Only the exception CLASS name is echoed (e.g. ConnectionTimeout) — raw
    transport internals never leak. There are no OpenSearch retries anywhere
    by design, so a failed search maps straight here.
    """
    return JSONResponse(
        status_code=502,
        content={
            "detail": f"OpenSearch search failed ({type(exc).__name__}).",
            "dependency": "opensearch",
        },
    )


def _handle_opensearch_not_ready(request: Request, exc: Exception) -> JSONResponse:
    """
    OpenSearch client unavailable/not-ready → 502 with the recorded reason.

    The reason is our own clean prose (e.g. the ``_meta`` guard's mismatch
    explanation or "endpoint not set"), never a raw exception.
    """
    return JSONResponse(
        status_code=502,
        content={"detail": str(exc), "dependency": "opensearch"},
    )


app.add_exception_handler(UnknownConfigError, _handle_unknown_config)
app.add_exception_handler(MalformedConfigRefError, _handle_malformed_config_ref)
app.add_exception_handler(ConfigIntegrityError, _handle_config_integrity)
app.add_exception_handler(BedrockThrottleExhaustedError, _handle_bedrock_throttle_exhausted)
app.add_exception_handler(ClientError, _handle_bedrock_client_error)
app.add_exception_handler(OpenSearchException, _handle_opensearch_failure)
app.add_exception_handler(OpenSearchNotReadyError, _handle_opensearch_not_ready)

app.include_router(query_router)
app.include_router(analyze_router)
app.include_router(compare_router)
app.include_router(health_router)
