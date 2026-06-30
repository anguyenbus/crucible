"""
CLI shell for replay evaluation (thin wrapper).

Usage:
    uv run eval-replay --candidate-spec configs/candidates/zvec.yaml

Parses args, validates the config, and delegates to the service library
``service/runners/replay.run_replay``. Replay is reserved Appendix-A machinery
(comparison stats live in ``crucible.kernel.replay_stats.comparison``).
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    """Parse args, validate config, call the replay service library."""
    import argparse

    from crucible.service.config import load_config
    from crucible.service.runners.replay import run_replay

    parser = argparse.ArgumentParser(
        description="Replay evaluation comparing candidate service against production baseline"
    )
    parser.add_argument("--candidate-spec", type=Path, required=True)
    parser.add_argument("--production-baseline", type=str, default="default")
    parser.add_argument("--config", type=Path, default=Path("eval_config.yaml"))
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    try:
        _ = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    result = run_replay(
        candidate_spec=args.candidate_spec,
        production_baseline=args.production_baseline,
    )
    print("eval-replay is reserved Appendix-A machinery (not yet fully implemented).")
    print(f"  Candidate spec: {result.candidate_spec}")
    print(f"  Production baseline: {result.production_baseline}")


if __name__ == "__main__":
    main()
