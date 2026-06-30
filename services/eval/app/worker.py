"""Worker entrypoint — the data plane. Run as a batch, never as a server.

  • nightly CronJob:   python -m app.worker --nightly
  • API-triggered:     python -m app.worker --run-id run_01J...   (K8s Job from dispatch.py)

Descendant of the dev run_rag_eval.py: read input -> build deps -> call a
runner -> write results. The heavy scoring imports are LAZY (inside _execute) so
importing this module stays cheap and the import-firewall stays one-directional.
"""
from __future__ import annotations

import argparse
import logging

from app.clients.store import RUNS
from app.schemas import EvalRun

log = logging.getLogger(__name__)


def _execute(run: EvalRun, project_id: str) -> None:
    """Dispatch on target.type → the right runner. Data-plane imports happen HERE."""
    RUNS.mark_running(run.run_id, project_id)
    try:
        if run.target.type.value == "dataset":
            # Lazy import of the scoring stack — allowed in the worker only.
            from app.runners.golden_set import run_phoenix_native  # noqa: F401

            log.info("would run golden-set scoring for %s (run_phoenix_native)", run.run_id)
            # real impl: build embedder + adapter + judge, call run_phoenix_native(...)
        else:
            log.info("target %s not wired in this scaffold", run.target.type)
        RUNS.complete(run.run_id, project_id)
    except Exception as e:  # noqa: BLE001
        RUNS.fail(run.run_id, project_id)
        log.error("run %s failed: %s", run.run_id, e)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Eval worker (data plane)")
    parser.add_argument("--run-id")
    parser.add_argument("--nightly", action="store_true")
    parser.add_argument("--project-id", default="proj_local")
    args = parser.parse_args()

    if args.nightly:
        log.info("nightly mode: would create its own run record and score a span window")
        print("worker: nightly entrypoint OK")
        return

    if not args.run_id:
        parser.error("--run-id is required unless --nightly")

    run = RUNS.get(args.run_id, args.project_id)
    if run is None:
        # In the real impl the record is in RDS (written by the API before dispatch);
        # the in-memory scaffold store does not span processes, so this is expected here.
        print(f"worker: no in-memory record for {args.run_id} (RDS-backed in real impl)")
        return

    _execute(run, args.project_id)
    print(f"worker: executed {args.run_id} -> {run.status.value}")


if __name__ == "__main__":
    main()
