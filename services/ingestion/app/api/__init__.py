"""API routers.

The ingest and search endpoint routers register themselves on `api_router`,
which `app.main.create_app` mounts on the application.
"""

from fastapi import APIRouter

from app.api.ingest import router as ingest_router
from app.api.search import router as search_router

api_router = APIRouter()
api_router.include_router(ingest_router)
api_router.include_router(search_router)
