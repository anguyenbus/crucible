"""Liveness/readiness probes (no auth)."""
from __future__ import annotations

from fastapi import APIRouter

health_router = APIRouter(tags=["health"])


@health_router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@health_router.get("/readyz")
def readyz() -> dict[str, str]:
    # SCAFFOLD: real impl checks RDS Proxy reachability (and the run queue, if used).
    return {"status": "ready"}
