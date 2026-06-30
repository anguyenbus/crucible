"""
CLI entry point for stub HTTP service.

This module provides the command-line interface for starting the stub service,
using Click for argument parsing and uvicorn for serving.
"""

from __future__ import annotations

import socket
from pathlib import Path

import click
from rich.console import Console


def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """
    Check if a port is available for binding.

    Args:
        port: Port number to check.
        host: Host address to bind to.

    Returns:
        True if port is available, False otherwise.

    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        return False


@click.command()
@click.option(
    "--config",
    "-c",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to stub configuration YAML file",
)
@click.option(
    "--port",
    "-p",
    type=int,
    default=None,
    help="Port to bind to (overrides config file)",
)
@click.option(
    "--corpus-dir",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Override corpus directory from config",
)
@click.option(
    "--phoenix-endpoint",
    type=str,
    default=None,
    help="Override Phoenix endpoint for distributed tracing",
)
@click.option(
    "--export-spans",
    type=str,
    default=None,
    help="Whether to export OpenInference spans to Phoenix (true/false)",
)
def cli(
    config: Path,
    port: int | None,
    corpus_dir: Path | None,
    phoenix_endpoint: str | None,
    export_spans: str | None,
) -> None:
    r"""
    Start the stub RAG HTTP service.

    Example:
        uv run python -m dev.stubs.service \
            --config configs/stubs/chunking-512.yaml --port 8081

    """
    from dev.stubs.service import create_app
    from dev.stubs.service.config import StubConfig

    console = Console()

    # Load stub configuration
    console.print(f"[info]Loading stub config from {config}[/info]")
    stub_config = StubConfig.from_yaml_file(config)

    # Apply CLI overrides
    if port is not None:
        stub_config.port = port

    if corpus_dir is not None:
        stub_config.corpus_path = corpus_dir.resolve()

    if phoenix_endpoint is not None:
        stub_config.phoenix_endpoint = phoenix_endpoint

    # Parse export_spans string to bool
    if export_spans is not None:
        stub_config.export_spans = export_spans.lower() in ("true", "1", "yes", "on")

    # Validate port availability
    if not is_port_available(stub_config.port):
        console.print(
            f"[error]Port {stub_config.port} is already in use. "
            f"Choose a different port or stop the conflicting service.[/error]"
        )
        raise click.Abort()

    # Create FastAPI app
    app = create_app(stub_config)

    # Print startup message
    console.print("\n[success]Stub RAG Service Starting[/success]")
    console.print(f"  Port: {stub_config.port}")
    console.print(f"  Corpus: {stub_config.resolved_corpus_path}")
    console.print(
        f"  Chunking: {stub_config.chunking_strategy} "
        f"(size={stub_config.chunk_size}, overlap={stub_config.chunk_overlap})"
    )
    console.print(f"  Embedding: {stub_config.embedding_model}")
    console.print(f"  Phoenix: {stub_config.phoenix_endpoint}")
    console.print(f"  Export Spans: {stub_config.export_spans}")
    console.print(f"  Health: http://127.0.0.1:{stub_config.port}/health")
    console.print(f"  Query: http://127.0.0.1:{stub_config.port}/query\n")

    # Start uvicorn server
    try:
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=stub_config.port, log_level="info")
    except KeyboardInterrupt:
        console.print("\n[info]Shutting down gracefully...[/info]")
    except Exception as e:
        console.print(f"[error]Server error: {e}[/error]")
        raise click.Abort() from e


def main() -> None:
    """Entry point for python -m invocation."""
    cli()


if __name__ == "__main__":
    main()
