"""
CLI runner for generating Phoenix spans from RAG queries.

Usage:
    uv run generate-spans --limit 10

This script generates sample RAG queries and exports their spans to Phoenix
for replay testing purposes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ====================================================================
# SECURITY: DISABLE THIRD-PARTY TELEMETRY
# ====================================================================
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    """Generate Phoenix spans from RAG queries."""
    import argparse

    from crucible.config import load_config

    parser = argparse.ArgumentParser(
        description="Generate Phoenix spans from RAG queries for replay testing"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of queries to process (default: 10)",
    )
    parser.add_argument(
        "--slice",
        choices=["pico", "nano", "full"],
        default="nano",
        help="Dataset slice (default: nano)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("eval_config.yaml"),
        help="Path to eval_config.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for span exports",
    )

    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("generate-spans is not yet fully implemented.")
    print("This is a placeholder for the span generation runner.")
    print(f"  Config: {args.config}")
    print(f"  Limit: {args.limit}")
    print(f"  Slice: {args.slice}")


if __name__ == "__main__":
    main()
