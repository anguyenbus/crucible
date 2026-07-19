"""API routers.

The parse endpoint router registers itself on `api_router`, which
`app.main.create_app` mounts on the application. Mirrors
`services/ingestion/app/api/__init__.py`.
"""

from fastapi import APIRouter

from app.api.parse import router as parse_router

api_router = APIRouter()
api_router.include_router(parse_router)
