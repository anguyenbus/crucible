"""
eval CLI main entry point.

This module provides the main CLI interface for the local ``eval`` command with
subcommands for RAG evaluation, span generation, replay testing, service serving,
and health checks.
"""

from __future__ import annotations

import click

from .check import check


@click.group()
@click.version_option(version="0.1.0", prog_name="eval")
def main() -> None:
    """
    eval: RAG evaluation and replay testing framework (local dev CLI).

    Typical usage:
        eval eval-rag --slice pico --rag stub-local --top_k 5
        eval generate-spans --limit 10
        eval eval-replay --candidate-spec ... --production-baseline
        eval serve --config ... --port 8082
        eval check phoenix
        eval check config
    """
    pass


# Import and register subcommands
main.add_command(check)
