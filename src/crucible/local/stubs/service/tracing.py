"""
Tracing integration for stub HTTP service.

This module handles OpenInference span emission and trace context propagation
for HTTP requests to the stub service.
"""

from __future__ import annotations

from typing import Any, Final

# Constants
DEFAULT_TRACER_NAME: Final[str] = "crucible.stub-service"


def extract_trace_context(headers: dict[str, str]) -> dict[str, str] | None:
    """
    Extract W3C trace context from HTTP headers.

    Args:
        headers: HTTP request headers.

    Returns:
        Dictionary with traceparent and tracestate if present, None otherwise.

    """
    trace_context = {}

    traceparent = headers.get("traceparent")
    if traceparent:
        trace_context["traceparent"] = traceparent

    tracestate = headers.get("tracestate")
    if tracestate:
        trace_context["tracestate"] = tracestate

    return trace_context if trace_context else None


def setup_phoenix_tracer(
    phoenix_endpoint: str | None,
    project_name: str = "case-assistant-synthetic",
) -> tuple[Any, Any] | tuple[None, None]:
    """
    Set up Phoenix tracer for distributed tracing.

    Args:
        phoenix_endpoint: Phoenix UI endpoint (e.g., http://localhost:6006).
        project_name: Phoenix project name for span export.

    Returns:
        Tuple of (tracer_provider, tracer) or (None, None) if disabled.

    """
    if phoenix_endpoint is None:
        return None, None

    from crucible.local.stubs.span_generator.tracer import setup_tracer

    return setup_tracer(
        phoenix_endpoint=phoenix_endpoint,
        project_name=project_name,
        batch=True,
        auto_instrument=False,
    )


__all__ = ["extract_trace_context", "setup_phoenix_tracer"]
