"""
Phoenix configuration module.

Provides configuration functions for Phoenix observability integration.
"""

from __future__ import annotations

import os
from typing import Any

PHOENIX_ENDPOINT_ENV: str = "PHOENIX_ENDPOINT"
DEFAULT_ENDPOINT: str = "http://localhost:6006"


def get_phoenix_config(
    config: dict[str, Any],
    cli_enabled: bool | None = None,
    cli_endpoint: str | None = None,
) -> dict[str, Any]:
    """
    Get Phoenix configuration from multiple sources with precedence.

    Precedence order (highest to lowest):
    1. CLI arguments (cli_* parameters)
    2. Environment variables (PHOENIX_ENDPOINT)
    3. YAML config (phoenix section from config dict)
    4. Defaults

    Args:
        config: Loaded configuration dictionary from eval_config.yaml.
        cli_enabled: CLI flag for enabling/disabling Phoenix.
        cli_endpoint: CLI-specified Phoenix endpoint.

    Returns:
        Dictionary with Phoenix configuration keys:
            - enabled: bool
            - endpoint: str
            - project_name: str
            - mode: str ("spans" or "native")
            - export_path: str

    """
    # Get phoenix section from YAML config
    phoenix_config = config.get("phoenix", {})

    # Resolve enabled flag (CLI > YAML > default)
    if cli_enabled is not None:
        enabled = cli_enabled
    else:
        enabled = phoenix_config.get("enabled", False)

    # Resolve endpoint (CLI > env var > YAML > default)
    if cli_endpoint is not None:
        endpoint = cli_endpoint
    else:
        # Check environment variable first
        env_endpoint = os.environ.get(PHOENIX_ENDPOINT_ENV)
        if env_endpoint:
            endpoint = env_endpoint
        else:
            endpoint = phoenix_config.get("endpoint", DEFAULT_ENDPOINT)

    # Resolve other config values
    project_name = phoenix_config.get("project_name", "crucible")
    mode = phoenix_config.get("mode", "spans")
    export_path = phoenix_config.get("export_path", "data/phoenix/spans.parquet")

    return {
        "enabled": enabled,
        "endpoint": endpoint,
        "project_name": project_name,
        "mode": mode,
        "export_path": export_path,
    }
