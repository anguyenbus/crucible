"""
Crucible CLI main entry point.

This module provides the main CLI interface for crucible with subcommands
for RAG evaluation, span generation, replay testing, service serving, and
health checks.
"""

from __future__ import annotations

import click

from .check import check


@click.group()
@click.version_option(version="0.1.0", prog_name="crucible")
def main() -> None:
    """
    Crucible: RAG evaluation and replay testing framework.

    Typical usage:
        crucible eval-rag --slice pico --rag stub-local --top_k 5
        crucible generate-spans --limit 10
        crucible eval-replay --candidate-spec ... --production-baseline
        crucible serve --config ... --port 8082
        crucible check phoenix
        crucible check config
    """
    pass


# Import and register subcommands
main.add_command(check)
