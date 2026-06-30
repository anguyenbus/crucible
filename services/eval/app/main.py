"""Control-plane entry point. `uvicorn app.main:app` (the Dockerfile CMD).

Thin: mounts routers, owns no business logic, imports NO scoring stack.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api import health_router, runs_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Evaluation Service — Internal API",
        version="1.0.0",
        description="Async control plane: POST /runs validates, persists, dispatches a worker, returns 202.",
    )
    # SCAFFOLD: real impl adds MtlsContextMiddleware (sets request.state.project_id)
    # and RequestLoggingMiddleware here.
    app.include_router(health_router)
    app.include_router(runs_router)
    return app


app = create_app()
