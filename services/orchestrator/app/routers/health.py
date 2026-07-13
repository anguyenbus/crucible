"""
Health probes: ``GET /healthz`` (liveness) and ``GET /readyz`` (readiness).

Phase 2 readiness semantic (the documented EVOLUTION of Phase 1's "config
manifest loaded and parseable" — same meaning, ready = able to serve
``/query``, now with real dependencies): the packaged config manifest loads,
the configured OpenSearch index exists (``indices.exists``), and the AWS
credential chain resolves credentials for Bedrock. Deliberately NO paid
invoke — readyz verifies reachability/credentials, NOT invoke permission
(that is proven by the first real generation in the acceptance run).

Results are TTL-cached (~20s) so orchestration-platform probe loops do not
hammer OpenSearch/STS. ``healthz`` stays pure process liveness and never
touches a dependency.
"""

from __future__ import annotations

import time
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import load_config_manifest

health_router = APIRouter(tags=["health"])

# Probe-result TTL: inside the spec's 15-30s window — long enough to absorb
# kubelet-style probe loops, short enough that recovery is seen quickly.
_READYZ_TTL_SECONDS: Final[float] = 20.0

# One cached readiness outcome: {"expires_at": monotonic, "ready": bool,
# "detail": str | None, "released_configs": int}. BOTH outcomes are cached —
# a down dependency is not re-probed on every 503 either.
_readyz_cache: dict[str, Any] = {}


def _reset_readyz_cache() -> None:
    """Drop the cached readiness outcome (tests; never called in serving)."""
    _readyz_cache.clear()


class HealthzResponse(BaseModel):
    """``GET /healthz`` body: process liveness only."""

    status: str = Field(description="Always 'ok' while the process serves requests.")


class ReadyzResponse(BaseModel):
    """``GET /readyz`` body: readiness to serve queries."""

    status: str = Field(description="'ready' when the readiness checks pass.")
    released_configs: int = Field(
        description="Number of released pipeline configs listed in the manifest.",
    )


def _check_readiness(state: Any) -> dict[str, Any]:
    """
    Run the real readiness checks against the lifespan-built clients.

    Returns a plain outcome dict (cache-friendly): ``ready`` plus a
    which-dependency ``detail`` when not ready.
    """
    try:
        manifest = load_config_manifest()
    except (FileNotFoundError, ValueError) as exc:
        return {"ready": False, "detail": f"config manifest unavailable: {exc}"}

    outcome: dict[str, Any] = {"ready": True, "released_configs": len(manifest)}

    clients = getattr(state, "clients", None)
    if clients is None:
        return {
            "ready": False,
            "detail": "clients not initialized (application lifespan has not run)",
        }

    if clients.search is None:
        # Covers: endpoint unset, _meta present-but-mismatched at init, and
        # OpenSearch unreachable at startup — the recorded reason says which.
        reason = clients.opensearch_unavailable_reason or "client unavailable"
        return {"ready": False, "detail": f"opensearch: {reason}"}
    try:
        if not clients.search.index_exists():
            return {"ready": False, "detail": "opensearch: configured index does not exist"}
    except Exception as exc:  # noqa: BLE001 — a probe failure IS the signal
        return {
            "ready": False,
            "detail": f"opensearch: index check failed ({type(exc).__name__})",
        }

    try:
        if not clients.bedrock.credentials_resolve():
            return {
                "ready": False,
                "detail": "bedrock: the AWS credential chain resolved no credentials",
            }
    except Exception as exc:  # noqa: BLE001 — a probe failure IS the signal
        return {
            "ready": False,
            "detail": f"bedrock: credential-chain check failed ({type(exc).__name__})",
        }

    return outcome


@health_router.get("/healthz", response_model=HealthzResponse)
def healthz() -> HealthzResponse:
    """Liveness probe: returns 200 whenever the process is serving."""
    return HealthzResponse(status="ok")


@health_router.get(
    "/readyz",
    response_model=ReadyzResponse,
    description=(
        "Readiness probe (ready = able to serve /query). Verifies that the "
        "packaged config manifest loads, that the configured OpenSearch "
        "index exists (indices.exists), and that the AWS credential chain "
        "resolves credentials for Bedrock. HONEST LIMIT: this verifies "
        "reachability and credentials only — it does NOT verify Bedrock "
        "invoke permission (no paid probe is made; invoke permission is "
        "proven by the first real generation). Results are cached for ~20 "
        "seconds. Any dependency down returns 503 with a detail naming the "
        "failing dependency."
    ),
)
def readyz(request: Request) -> ReadyzResponse:
    """Readiness probe: manifest + OpenSearch index + Bedrock credential chain."""
    now = time.monotonic()
    outcome = _readyz_cache.get("outcome")
    if outcome is None or now >= _readyz_cache["expires_at"]:
        outcome = _check_readiness(request.app.state)
        _readyz_cache["outcome"] = outcome
        _readyz_cache["expires_at"] = now + _READYZ_TTL_SECONDS

    if not outcome["ready"]:
        raise HTTPException(status_code=503, detail=outcome["detail"])
    return ReadyzResponse(status="ready", released_configs=outcome["released_configs"])
