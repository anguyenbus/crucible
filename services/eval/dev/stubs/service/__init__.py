"""
Stub HTTP service for RAG evaluation.

This module provides a FastAPI-based HTTP service that wraps the ChromaDB RAG pipeline,
enabling eval to test against services running as separate HTTP processes.
"""

from __future__ import annotations

from fastapi import FastAPI

from dev.stubs.service.config import StubConfig


def create_app(config: StubConfig) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: Stub configuration for this service instance.

    Returns:
        Configured FastAPI application.

    """
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(
        title="RAG Stub Service",
        description="HTTP service for RAG evaluation",
        version="1.0.0",
    )

    # Add CORS middleware for development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, specify allowed origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Setup Phoenix tracer for span emission (only if export_spans is enabled)
    from dev.stubs.service.config import DEFAULT_PROJECT_NAME
    from dev.stubs.service.tracing import setup_phoenix_tracer

    if config.export_spans:
        tracer_provider, tracer = setup_phoenix_tracer(
            phoenix_endpoint=config.phoenix_endpoint,
            project_name=DEFAULT_PROJECT_NAME,
        )
    else:
        tracer_provider, tracer = None, None

    app.state.tracer = tracer
    app.state.tracer_provider = tracer_provider
    app.state.export_spans = config.export_spans

    # Import and include routes
    from dev.stubs.service.endpoints import router

    app.include_router(router)

    # Store config in app state for access in endpoints
    app.state.config = config

    return app


__all__ = ["create_app"]
