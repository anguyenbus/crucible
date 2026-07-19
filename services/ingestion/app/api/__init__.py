"""API routers.

The ingest, search, index-lifecycle, and document-chunks endpoint routers
register themselves on `api_router`, which `app.main.create_app` mounts on the
application.
"""

from fastapi import APIRouter

from app.api.document_chunks import router as document_chunks_router
from app.api.indices import router as indices_router
from app.api.ingest import router as ingest_router
from app.api.search import router as search_router

api_router = APIRouter()
api_router.include_router(ingest_router)
api_router.include_router(search_router)
api_router.include_router(indices_router)
api_router.include_router(document_chunks_router)
