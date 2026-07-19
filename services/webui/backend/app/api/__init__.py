"""API router aggregation for the webui BFF."""

from fastapi import APIRouter

from app.api.documents import router as documents_router
from app.api.projects import router as projects_router

api_router = APIRouter()
api_router.include_router(projects_router)
api_router.include_router(documents_router)
