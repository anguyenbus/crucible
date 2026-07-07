"""FastAPI application factory: liveness endpoint + API router wiring."""

from fastapi import FastAPI

from app.api import api_router


def create_app() -> FastAPI:
    app = FastAPI(title="Markdown Ingestion + Hybrid Search Service")
    app.include_router(api_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
