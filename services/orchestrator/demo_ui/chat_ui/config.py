"""
Env surface of the demo UI (read at call time so tests can monkeypatch).

- ``ORCHESTRATOR_URL`` — orchestrator base URL (default
  ``http://localhost:8000``). The UI is HTTP-only: this is its ONLY channel
  to the pipeline.
- ``PHOENIX_ENDPOINT`` — Phoenix UI endpoint (e.g. ``http://localhost:6006``)
  for the UI's own OTLP-HTTP exporter and the rendered trace links. Unset →
  a genuine no-op tracer; the UI stays fully functional.
- ``DEMO_UI_PIPELINE_CONFIG`` — pinned pipeline config ref sent on every
  request (default ``legal-rag-default-1.3.0``, the system-prompt-leakage
  guard-enabled config; eval's lane stays on ``legal-rag-default-1.1.0``
  untouched). A leak/injection attempt is refused verbatim; a benign question
  streams and cites as before.
"""

from __future__ import annotations

import os

DEFAULT_ORCHESTRATOR_URL = "http://localhost:8000"
DEFAULT_PIPELINE_CONFIG = "legal-rag-default-1.3.0"


def orchestrator_url() -> str:
    """Orchestrator base URL (no trailing slash)."""
    return os.environ.get("ORCHESTRATOR_URL", DEFAULT_ORCHESTRATOR_URL).rstrip("/")


def phoenix_endpoint() -> str | None:
    """Phoenix endpoint, or None when unset/empty (→ no-op tracer, no links)."""
    return os.environ.get("PHOENIX_ENDPOINT") or None


def pipeline_config_ref() -> str:
    """Return the single pinned config ref this UI queries with."""
    return os.environ.get("DEMO_UI_PIPELINE_CONFIG", DEFAULT_PIPELINE_CONFIG)
