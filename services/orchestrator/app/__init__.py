"""
Orchestrator service package (Phase 1: contracts-first).

This is the deliverable that migrates 1:1 to
``genai-backend/services/orchestrator/app/``. It holds the FastAPI control
plane (``app.main``, ``app.routers``), the pipeline stage stubs
(``app.orchestrator``), infra client seams (``app.clients``), Pydantic models
(``app.schemas``), the packaged schema mirror (``app.contracts``), the pinned
pipeline configs (``app.configs``), and the env-driven ``app.config``.

Importing this package performs NO environment reads and NO I/O.
"""

__version__ = "0.1.0"
