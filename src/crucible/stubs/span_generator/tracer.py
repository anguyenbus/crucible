"""
OpenTelemetry tracer setup for synthetic span generation.

This module provides tracer initialization using Phoenix's OTLP exporter.
Uses a separate tracer instance from PhoenixAdapter to maintain
correct dependency direction.
"""

from __future__ import annotations

import re

from beartype import beartype
from beartype.typing import Any, Final

# Constants
DEFAULT_ENDPOINT: Final[str] = "http://localhost:6006"
DEFAULT_PROJECT_NAME: Final[str] = "case-assistant-synthetic"
DEFAULT_BATCH: Final[bool] = True
DEFAULT_AUTO_INSTRUMENT: Final[bool] = False


@beartype
def _get_otlp_endpoint(ui_endpoint: str) -> str:
    """
    Convert UI endpoint to OTLP HTTP endpoint.

    Phoenix accepts OTLP via HTTP at:
    - http://localhost:6006/v1/traces (UI port + /v1/traces path)

    Phoenix 4.x uses HTTP by default. gRPC (4317) requires additional setup.

    Args:
        ui_endpoint: Phoenix UI endpoint (e.g., http://localhost:6006).

    Returns:
        OTLP HTTP endpoint URL (e.g., http://localhost:6006/v1/traces).

    """
    # Use HTTP endpoint on UI port
    match = re.match(r"(https?://[^:]+):\d+", ui_endpoint)
    if match:
        return f"{match.group(1)}:6006/v1/traces"
    return "http://localhost:6006/v1/traces"


@beartype
def setup_tracer(
    phoenix_endpoint: str = DEFAULT_ENDPOINT,
    project_name: str = DEFAULT_PROJECT_NAME,
    batch: bool = DEFAULT_BATCH,
    auto_instrument: bool = DEFAULT_AUTO_INSTRUMENT,
) -> tuple[Any, Any]:
    """
    Initialize OpenTelemetry tracer with Phoenix OTLP exporter.

    Uses phoenix.otel.register() to configure OTLP export via gRPC.
    Returns both the tracer provider and tracer instance.

    Args:
        phoenix_endpoint: Phoenix UI endpoint (e.g., http://localhost:6006).
        project_name: Phoenix project name for grouping traces.
        batch: Whether to use batch span processing.
        auto_instrument: Whether to enable auto-instrumentation (False for manual).

    Returns:
        Tuple of (tracer_provider, tracer) or (None, None) if initialization fails.

    Raises:
        ImportError: If Phoenix SDK is not installed.

    Example:
        >>> tracer_provider, tracer = setup_tracer()
        >>> with tracer.start_as_current_span("test"):
        ...     pass

    """
    try:
        from phoenix.otel import register
    except ImportError as e:
        raise ImportError(
            "Phoenix SDK not installed. "
            "Install with: uv pip install eval-harness[replay]"
        ) from e

    try:
        # Convert UI endpoint to OTLP HTTP endpoint
        otlp_endpoint = _get_otlp_endpoint(phoenix_endpoint)

        # Register with Phoenix using HTTP/protobuf protocol
        tracer_provider = register(
            endpoint=otlp_endpoint,
            project_name=project_name,
            protocol="http/protobuf",  # HTTP with protobuf payload
            batch=batch,
            set_global_tracer_provider=True,  # Enable global access via trace.get_tracer()
        )

        # Get tracer from provider
        tracer = tracer_provider.get_tracer("eval-harness.span-generator")

        return tracer_provider, tracer

    except Exception as e:
        import sys

        print(
            f"[WARN] Phoenix tracer initialization failed: {e}. "
            "Span export will be disabled.",
            file=sys.stderr,
        )
        return None, None
