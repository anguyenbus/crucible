"""
CLI runner for replay evaluation.

Usage:
    uv run eval-replay --candidate-spec configs/candidates/zvec.yaml --production-baseline

This script replays production traces against a candidate service and compares
the results using statistical tests.
"""

from __future__ import annotations

import os
import sys

# ====================================================================
# SECURITY: DISABLE THIRD-PARTY TELEMETRY
# ====================================================================
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"

from dotenv import load_dotenv
from pathlib import Path

load_dotenv()


def main() -> None:
    """Run replay evaluation comparing candidate vs baseline."""
    import argparse

    from crucible.config import load_config

    parser = argparse.ArgumentParser(
        description="Replay evaluation comparing candidate service against production baseline"
    )
    parser.add_argument(
        "--candidate-spec",
        type=Path,
        required=True,
        help="Path to candidate service specification YAML",
    )
    parser.add_argument(
        "--production-baseline",
        type=str,
        default="default",
        help="Phoenix project name for production baseline traces",
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
        help="Output directory for comparison results",
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

    print("eval-replay is not yet fully implemented.")
    print("This is a placeholder for the replay evaluation runner.")
    print(f"  Candidate spec: {args.candidate_spec}")
    print(f"  Production baseline: {args.production_baseline}")


if __name__ == "__main__":
    main()
