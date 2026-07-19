"""FastAPI application factory for the webui BFF.

Two-backend topology (recorded here on purpose): the frontend talks to TWO
backends. Chat STAYS on the Phase-1 Next.js route-handler proxy to the
orchestrator -- this BFF does NOT proxy `/query/stream` and owns ONLY
projects/documents/upload/ingestion. Unlike the orchestrator (no CORS, so it
sits behind the Next proxy), the BFF is purpose-built WITH CORS so the browser
can reach it directly via `NEXT_PUBLIC_BFF_URL`.

Dependency firewall: sibling services are consumed over their env-var URLs ONLY
(`WEBUI_INGESTION_URL` / `WEBUI_ORCHESTRATOR_URL`). Nothing here imports the
`services.orchestrator` or `services.ingestion` packages.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import api_router
from app.config import get_settings
from app.db import init_db
from app.health import check_ingestion_ready


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(get_settings().db_path)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Document Analyser BFF", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        ready, detail = check_ingestion_ready()
        status = "ready" if ready else "not_ready"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": status, "detail": detail},
        )

    return app


app = create_app()
