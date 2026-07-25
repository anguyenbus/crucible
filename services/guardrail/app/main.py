"""
FastAPI entry point for the guardrail pod.

Phase 1 (skeleton): exposes ``POST /check/input``, ``POST /check/output``,
``GET /healthz`` (+ ``GET /readyz``). The ONE reusable :class:`LLMRails` is built
ONCE in the lifespan — the framework shim (``bedrock_engine.force_langchain_framework``)
runs BEFORE construction so Bedrock's only path is selected — and reused per
request; there is NO per-request rails rebuild. Modeled on the orchestrator's
``app/main.py`` construct-once discipline.

Phase 0 (deterministic detector): the lifespan ALSO compiles the deterministic
secrets/PII detector (``config/detectors.yml``) ONCE. If ANY pattern fails to
compile, the detector slot is left unset and ``/readyz`` reports 503 — a broken
detector must never silently no-op (fail-fast readiness).

Importing this module performs NO environment reads and NO I/O: Settings, the
rails engine, and the detector all resolve at lifespan. Tests never reach AWS —
lifespan fills each ``app.state`` slot ONLY when absent, so a mock ``rails`` /
``settings`` / ``detectors`` pre-installed before startup wins (the substitution
seam).

ALL error mapping lives HERE, at app level, via ``add_exception_handler`` — the
routes raise and never carry per-route try/except. A rail-invocation error
returns a CLEAN JSON 500 (dependency named, never a leaked stack), and the
orchestrator's per-rail fail policy (Q2) decides block-vs-flag from the failure.

Run locally with: ``uv run uvicorn app.main:app``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.bedrock_engine import build_rails
from app.chunk_scan import ChunkScanConfigError, load_chunk_scanner
from app.config_digest import verify_pinned_digests
from app.detectors import DetectorConfigError, load_detectors
from app.routers.check import check_router
from app.routers.health import health_router
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Construct Settings, the ONE reusable LLMRails engine, and the detector.

    Each slot is filled only when absent so tests can pre-install mocks on
    ``app.state`` (the substitution seam); a pre-set slot is never rebuilt.
    """
    if getattr(app.state, "settings", None) is None:
        app.state.settings = get_settings()
    settings = app.state.settings
    # Two-hash determinism self-verification (Phase 4 / I4): if the pod was
    # pinned to a selector (expected digests injected by env), assert its LIVE
    # config/ digest + uv.lock sha match — else REFUSE to serve (raise), so a
    # drifted image cannot answer under a pin. No-op in dev (no expectation set).
    # Runs BEFORE the engine build so a mismatch fails fast without touching
    # Bedrock.
    verify_pinned_digests(
        config_dir=settings.config_dir,
        lock_path=settings.lock_path,
        expected_config_dir_digest=settings.expected_config_dir_digest,
        expected_uv_lock_sha256=settings.expected_uv_lock_sha256,
    )
    # Compile the deterministic secrets/PII detector ONCE. A non-compiling pattern
    # is NOT a silent no-op: leave the slot None so /readyz fails fast (503). The
    # error is stashed for the probe body (loud, not silent).
    if getattr(app.state, "detectors", None) is None:
        try:
            app.state.detectors = load_detectors(settings.config_dir)
            app.state.detectors_error = None
        except DetectorConfigError as exc:
            app.state.detectors = None
            app.state.detectors_error = str(exc)
    # Compile the deterministic injection scanner ONCE (the ingest-time
    # /check/chunks lane). Same fail-fast discipline as the secrets detector: a
    # non-compiling injection pattern leaves the slot None so /readyz reports 503,
    # never a silently dead corpus-poisoning guard.
    if getattr(app.state, "chunk_scanner", None) is None:
        try:
            app.state.chunk_scanner = load_chunk_scanner(settings.config_dir)
            app.state.chunk_scanner_error = None
        except ChunkScanConfigError as exc:
            app.state.chunk_scanner = None
            app.state.chunk_scanner_error = str(exc)
    if getattr(app.state, "rails", None) is None:
        app.state.rails = build_rails(settings.config_dir)
    yield


app = FastAPI(
    title="guardrail",
    version="0.1.0",
    description=(
        "Out-of-process NeMo Guardrails pod backed EXCLUSIVELY by AWS Bedrock "
        "Haiku (no OpenAI, no NIM). POST /check/input and POST /check/output "
        "return plain-data verdicts the orchestrator maps onto its "
        "ClassifierVerdict shape; the ONE LLMRails engine is built once in "
        "lifespan and reused per request."
    ),
    lifespan=lifespan,
)


def _handle_bedrock_client_error(request: Request, exc: Exception) -> JSONResponse:
    """
    Bedrock ``ClientError`` during a guard call → 502 naming the dependency.

    Only the AWS error CODE is echoed (machine-usable) — the raw message never
    leaks. The orchestrator treats a 5xx from the pod per its fail policy.
    """
    response = getattr(exc, "response", None)
    code = "unknown"
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "unknown")
    return JSONResponse(
        status_code=502,
        content={
            "detail": f"Bedrock guard call failed (AWS error code: {code}).",
            "dependency": "bedrock",
        },
    )


def _handle_rail_error(request: Request, exc: Exception) -> JSONResponse:
    """
    Any other rail-invocation failure → CLEAN 500 (never a leaked stack).

    Only the exception CLASS name is echoed; the orchestrator's per-rail fail
    policy (input fail-SAFE block / output-facts fail-OPEN flag) decides the
    user-facing outcome from this failure.
    """
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"guard rail invocation failed ({type(exc).__name__}).",
            "dependency": "guardrail",
        },
    )


app.add_exception_handler(ClientError, _handle_bedrock_client_error)
app.add_exception_handler(Exception, _handle_rail_error)

app.include_router(check_router)
app.include_router(health_router)
