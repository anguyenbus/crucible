"""FastAPI application factory: liveness endpoint + API router wiring.

Mirrors `services/ingestion/app/main.py`. The parser is internal-only /
east-west (never ingress-exposed, no app-level auth) — an exposed parser billing
Textract per page would be a cost-bomb / DoS.
"""

from fastapi import FastAPI

from app.api import api_router


def create_app() -> FastAPI:
    app = FastAPI(title="Document Parser Service (docling + Textract, HTTP-isolated)")
    app.include_router(api_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
