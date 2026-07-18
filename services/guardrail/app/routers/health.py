"""
Health probes: ``GET /healthz`` (liveness) and ``GET /readyz`` (readiness).

Modeled on the orchestrator's ``app/routers/health.py``. The pod must survive
the independent-K8s-pod model (compose is dev-only, not the only wiring), so:

- ``/healthz`` is pure process liveness — 200 whenever the process serves,
  touching no dependency (never a Bedrock call).
- ``/readyz`` reflects that the ONE reusable :class:`LLMRails` was constructed in
  lifespan (``app.state.rails`` is not None) AND the deterministic secrets/PII
  detector compiled (``app.state.detectors`` is not None). A non-compiling
  detector pattern FAILS readiness (503) — a broken detector must never silently
  no-op. Deliberately NO paid Bedrock probe — readiness verifies the guard engine
  is built + the detector compiled, not invoke permission (proven by the first
  real ``/check`` call).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

health_router = APIRouter(tags=["health"])


class HealthzResponse(BaseModel):
    """``GET /healthz`` body: process liveness only."""

    status: str = Field(description="Always 'ok' while the process serves requests.")


class ReadyzResponse(BaseModel):
    """``GET /readyz`` body: readiness to serve guard checks."""

    status: str = Field(
        description="'ready' when the LLMRails engine is constructed AND the "
        "deterministic detector compiled."
    )


@health_router.get("/healthz", response_model=HealthzResponse)
def healthz() -> HealthzResponse:
    """Liveness probe: returns 200 whenever the process is serving."""
    return HealthzResponse(status="ok")


@health_router.get(
    "/readyz",
    response_model=ReadyzResponse,
    description=(
        "Readiness probe (ready = able to serve /check). Verifies the ONE "
        "reusable LLMRails engine was constructed in lifespan AND every "
        "deterministic detector pattern compiled. A non-compiling detector "
        "pattern FAILS readiness (503) so a broken detector never silently "
        "no-ops. HONEST LIMIT: no paid Bedrock probe is made — invoke permission "
        "is proven by the first real /check call. Returns 503 with a reason when "
        "the engine or the detector is absent."
    ),
)
def readyz(request: Request) -> ReadyzResponse:
    """Readiness probe: the LLMRails engine was built AND the detector compiled."""
    rails = getattr(request.app.state, "rails", None)
    if rails is None:
        raise HTTPException(
            status_code=503,
            detail="guard engine not initialized (application lifespan has not run)",
        )
    detectors = getattr(request.app.state, "detectors", None)
    if detectors is None:
        reason = getattr(request.app.state, "detectors_error", None)
        detail = "deterministic detector failed to compile"
        if reason:
            detail = f"{detail}: {reason}"
        raise HTTPException(status_code=503, detail=detail)
    return ReadyzResponse(status="ready")
