"""
Health check commands for crucible dependencies.

This module provides CLI commands to verify connectivity and configuration
of external services (Phoenix, ChromaDB, etc.) before running evaluations.
"""

from __future__ import annotations

import os
import re
import urllib.request

import click


@click.group()
def check() -> None:
    """Check connectivity to external services."""
    pass


@check.command()
@click.option(
    "--endpoint",
    envvar="PHOENIX_ENDPOINT",
    default="http://localhost:6006",
    help="Phoenix server endpoint (default: $PHOENIX_ENDPOINT or http://localhost:6006)",
)
@click.option(
    "--timeout",
    default=5,
    help="Connection timeout in seconds (default: 5)",
)
def phoenix(endpoint: str, timeout: int) -> None:
    """
    Check Phoenix server connectivity.

    Verifies that Phoenix is reachable and responding. This is useful
    for pre-flight checks before running evaluations.

    Examples:
        crucible check phoenix
        crucible check phoenix --endpoint https://phoenix.prod.example.com
        crucible check phoenix --endpoint http://localhost:6006 --timeout 10

    Exit codes:
        0: Phoenix is reachable
        1: Phoenix is not reachable or connection error

    """
    click.echo(f"Checking Phoenix at: {endpoint}")

    # Try to reach the UI endpoint
    ui_url = endpoint.rstrip("/")

    # Try to reach the /health endpoint if available
    # (Phoenix doesn't have a standard /health, so we check the UI)
    try:
        req = urllib.request.Request(
            ui_url,
            method="GET",
            headers={"User-Agent": "crucible/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                click.echo(
                    click.style("OK Phoenix is reachable", fg="green", bold=True)
                )
                click.echo(f"  UI: {ui_url}")
                click.echo(f"  OTLP: {_get_otlp_endpoint(ui_url)}")
                return  # Exit 0
            else:
                click.echo(
                    click.style(
                        f"ERROR Phoenix returned status {response.status}",
                        fg="red",
                    ),
                    err=True,
                )
                raise SystemExit(1)
    except urllib.error.HTTPError as e:
        # Phoenix might not have a proper / endpoint but still be up
        # Try the OTLP endpoint as well
        otlp_url = _get_otlp_endpoint(ui_url)
        try:
            req = urllib.request.Request(
                otlp_url,
                method="POST",
                headers={"User-Agent": "crucible/1.0"},
                data=b"{}",  # Empty payload
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                click.echo(
                    click.style("OK Phoenix OTLP endpoint is reachable", fg="green")
                )
                click.echo(f"  UI: {ui_url}")
                click.echo(f"  OTLP: {otlp_url}")
                return
        except Exception:
            click.echo(
                click.style(f"ERROR Phoenix HTTP error: {e.code}", fg="red"),
                err=True,
            )
            raise SystemExit(1)
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "Connection refused" in reason or "connect" in reason.lower():
            click.echo(
                click.style(
                    f"ERROR Connection refused - Phoenix may not be running at {endpoint}",
                    fg="red",
                ),
                err=True,
            )
        elif "timeout" in reason.lower():
            click.echo(
                click.style(
                    "ERROR Connection timeout - Phoenix may be behind a firewall",
                    fg="red",
                ),
                err=True,
            )
        else:
            click.echo(
                click.style(f"ERROR Connection error: {reason}", fg="red"),
                err=True,
            )
        raise SystemExit(1)
    except Exception as e:
        click.echo(
            click.style(f"ERROR Unexpected error: {e}", fg="red"),
            err=True,
        )
        raise SystemExit(1)


def _get_otlp_endpoint(ui_endpoint: str) -> str:
    """
    Convert UI endpoint to OTLP HTTP endpoint.

    Phoenix accepts OTLP via HTTP at:
    - http://localhost:6006/v1/traces (UI port + /v1/traces path)

    Args:
        ui_endpoint: Phoenix UI endpoint (e.g., http://localhost:6006).

    Returns:
        OTLP HTTP endpoint URL (e.g., http://localhost:6006/v1/traces).

    """
    # Parse the UI endpoint
    # For https://phoenix.prod.example.com:6006 -> https://phoenix.prod.example.com:6006/v1/traces
    # For https://phoenix.prod.example.com -> https://phoenix.prod.example.com/v1/traces (assumes port 443)
    # For http://localhost:6006 -> http://localhost:6006/v1/traces

    match = re.match(r"(https?://[^/]+)", ui_endpoint)
    if match:
        base = match.group(1)
        return f"{base}/v1/traces"
    return "http://localhost:6006/v1/traces"


@check.command()
@click.option(
    "--endpoint",
    envvar="PHOENIX_ENDPOINT",
    default="http://localhost:6006",
    help="Phoenix server endpoint",
)
@click.option(
    "--project",
    default="default",
    help="Phoenix project name to check",
)
@click.pass_context
def config(ctx: click.Context, endpoint: str, project: str) -> None:
    """
    Display current Phoenix configuration.

    Shows how the Phoenix endpoint is resolved and what will be used
    for span export.
    """
    click.echo("Phoenix Configuration:")
    env_var = os.environ.get("PHOENIX_ENDPOINT")
    click.echo(
        f"  Environment variable (PHOENIX_ENDPOINT): "
        f"{click.style(env_var or '(not set)', fg='blue' if env_var else 'black')}"
    )
    click.echo(f"  Effective endpoint: {click.style(endpoint, fg='green', bold=True)}")
    click.echo(f"  Project name: {project}")
    click.echo(f"  OTLP endpoint: {_get_otlp_endpoint(endpoint)}")

    # Warn about localhost
    if "localhost" in endpoint or "127.0.0.1" in endpoint:
        click.echo(
            click.style(
                "\nWARNING: Using localhost - this will only work on the same machine.",
                fg="yellow",
            )
        )
