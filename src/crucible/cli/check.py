"""
Health check commands for crucible dependencies.

This module provides CLI commands to verify connectivity and configuration
of external services (Phoenix, ChromaDB, etc.) before running evaluations.

It also provides the Bedrock startup preflight: a single cheap bedrock-runtime
call that fails fast and loud on missing credentials, an unset/mismatched region,
or model-access not granted, so that a broken Bedrock plumbing path is never
mistaken for a low-quality score later in the run.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Final

import click

# ====================================================================
# BEDROCK STARTUP PREFLIGHT
# ====================================================================
# When the resolved generator/judge provider is bedrock, run ONE cheap
# bedrock-runtime invoke_model call at startup to fail fast on:
#   - missing/incomplete credentials,
#   - an unset region (handled upstream by _resolve_region, which raises),
#   - the model not being available in the region, or
#   - model access not being granted to the account.
# This is the ONLY config-validation pulled in now; the full validation
# framework is out of scope. Distinct, actionable messages are emitted per
# error class where the botocore error/ClientError code allows.

# A 1-token Anthropic Claude invoke body: the cheapest call that still
# exercises creds + region + model access on the real bedrock-runtime API.
_PREFLIGHT_MAX_TOKENS: Final[int] = 1


class BedrockPreflightError(RuntimeError):
    """
    Raised when the Bedrock startup preflight fails.

    The message is actionable and distinct per failure class (missing creds vs
    access-not-granted vs model-not-in-region). The originating exception is
    chained so the underlying botocore error code stays inspectable.
    """


def _preflight_request_body(model: str) -> dict[str, Any]:
    """
    Build the cheapest invoke_model body for the preflight call.

    Anthropic Claude profiles (the au.* defaults) use the Messages format; any
    other family falls back to the Titan-style text body. Either way the call
    requests a single output token so it stays cheap.
    """
    if "anthropic." in model:
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": _PREFLIGHT_MAX_TOKENS,
            "messages": [{"role": "user", "content": "ping"}],
        }
    return {
        "inputText": "ping",
        "textGenerationConfig": {"maxTokenCount": _PREFLIGHT_MAX_TOKENS},
    }


def bedrock_preflight() -> None:
    """
    Run the Bedrock startup preflight when the generator provider is bedrock.

    No-op when the resolved provider is not bedrock (e.g. an OpenAI opt-in run);
    OpenAI has no equivalent cheap-call preflight in this spec.

    Reuses the existing region + credential-chain wiring from the generator:
    _resolve_generator_provider_and_model, _resolve_region, and the no-creds
    boto3.client("bedrock-runtime", ...) construction. Makes exactly ONE cheap
    1-token invoke_model call.

    Raises:
        BedrockPreflightError: with a distinct, actionable message on missing
            credentials, access-not-granted, or model-not-in-region. The
            originating botocore exception is chained.
        ValueError: propagated from region resolution when AWS_REGION /
            AWS_DEFAULT_REGION is unset (fail-loud, no us-east-1 default).

    """
    from crucible.stubs.rag.generator import (
        _client_error_code,
        _resolve_generator_provider_and_model,
        _resolve_region,
    )

    provider, model = _resolve_generator_provider_and_model()
    if provider != "bedrock":
        # No-op for non-bedrock providers (OpenAI opt-in path).
        return

    import boto3
    from botocore.exceptions import (
        ClientError,
        NoCredentialsError,
        PartialCredentialsError,
    )

    # Region resolution fails loud (ValueError) when unset; let it propagate so
    # the unset-region case is surfaced before any network call is attempted.
    region = _resolve_region()

    # No explicit credentials: the standard AWS credential chain engages.
    client = boto3.client("bedrock-runtime", region_name=region)

    try:
        client.invoke_model(
            modelId=model,
            body=json.dumps(_preflight_request_body(model)),
        )
    except (NoCredentialsError, PartialCredentialsError) as err:
        raise BedrockPreflightError(
            "Bedrock preflight failed: AWS credentials are missing or "
            "incomplete. Configure the AWS credential chain (e.g. AWS_PROFILE, "
            "an instance/role profile, or AWS_ACCESS_KEY_ID/"
            "AWS_SECRET_ACCESS_KEY). No credentials are passed in code by "
            "design."
        ) from err
    except ClientError as err:
        code = _client_error_code(err)
        if code == "AccessDeniedException":
            raise BedrockPreflightError(
                f"Bedrock preflight failed: access to model {model!r} is not "
                f"granted for this account in region {region!r}. Request model "
                "access in the Bedrock console (Model access) and ensure your "
                "IAM principal allows bedrock:InvokeModel."
            ) from err
        if code in ("ValidationException", "ResourceNotFoundException"):
            raise BedrockPreflightError(
                f"Bedrock preflight failed: model {model!r} is not available "
                f"in region {region!r} (or the model/inference-profile ID is "
                "invalid there). Use an ID supported in this region and ensure "
                "AWS_REGION matches the inference-profile geography (e.g. "
                "ap-southeast-2 for au.* profiles)."
            ) from err
        # Any other ClientError: re-raise UNCHANGED so its code stays
        # inspectable (do not flatten into a generic message).
        raise


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
                click.echo(click.style("OK Phoenix is reachable", fg="green", bold=True))
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
                raise SystemExit(1) from None
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
                click.echo(click.style("OK Phoenix OTLP endpoint is reachable", fg="green"))
                click.echo(f"  UI: {ui_url}")
                click.echo(f"  OTLP: {otlp_url}")
                return
        except Exception:
            click.echo(
                click.style(f"ERROR Phoenix HTTP error: {e.code}", fg="red"),
                err=True,
            )
            raise SystemExit(1) from None
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
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(
            click.style(f"ERROR Unexpected error: {e}", fg="red"),
            err=True,
        )
        raise SystemExit(1) from None


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
    # host:6006            -> host:6006/v1/traces
    # host (no port)       -> host/v1/traces (assumes port 443)
    # http://localhost:6006 -> http://localhost:6006/v1/traces

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


@check.command()
def bedrock() -> None:
    """
    Run the Bedrock startup preflight.

    Makes ONE cheap bedrock-runtime invoke_model call (1 output token) to fail
    fast on missing credentials, an unset/mismatched region, or model access
    not being granted. No-op (and reports as such) when the resolved generator
    provider is not bedrock.

    Exit codes:
        0: provider!=bedrock (skipped) or the preflight call succeeded
        1: the preflight failed (creds / region / model-access)

    """
    from crucible.stubs.rag.generator import _resolve_generator_provider_and_model

    provider, model = _resolve_generator_provider_and_model()
    if provider != "bedrock":
        click.echo(
            click.style(
                f"SKIP Bedrock preflight (provider={provider}, not bedrock)",
                fg="yellow",
            )
        )
        return

    click.echo(f"Running Bedrock preflight for model: {model}")
    try:
        bedrock_preflight()
    except (BedrockPreflightError, ValueError) as e:
        click.echo(click.style(f"ERROR {e}", fg="red"), err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.echo(
            click.style(f"ERROR Bedrock preflight failed: {e}", fg="red"),
            err=True,
        )
        raise SystemExit(1) from None

    click.echo(click.style("OK Bedrock preflight passed", fg="green", bold=True))
