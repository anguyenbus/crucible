"""Run ledger.

SCAFFOLD: in-memory implementation so the control plane is runnable without RDS.
The real implementation backs onto RDS (system of record); the method surface
(persist / get / mark_running / complete / fail) is what the worker and API share.
"""
from __future__ import annotations

import hashlib
import secrets

from app.schemas import EvalRun, EvalRunRequest, InitiatedBy, ResultCounts, RunStatus

# Crockford base32 (ULID alphabet) — no external dependency for the scaffold.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(26))
    return f"{prefix}_{body}"


def _repro_key(body: EvalRunRequest) -> str:
    blob = body.model_dump_json().encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()[:32]


class RunStore:
    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], EvalRun] = {}

    def persist(self, body: EvalRunRequest, project_id: str) -> EvalRun:
        """Write a 'queued' record (committed before dispatch in the real impl)."""
        run_id = new_id("run")
        run = EvalRun(
            run_id=run_id,
            status=RunStatus.queued,
            initiated_by=InitiatedBy.api,
            target=body.target,
            config_id=body.config_id,
            dataset_id=body.dataset_id,
            repro_key=_repro_key(body),
            result_counts=ResultCounts(),
            links={
                "self": f"/v1/eval/runs/{run_id}",
                "results": f"/v1/eval/runs/{run_id}/results",
            },
        )
        self._runs[(project_id, run_id)] = run
        return run

    def get(self, run_id: str, project_id: str) -> EvalRun | None:
        """project_id scopes the read — cross-project returns None -> 404."""
        return self._runs.get((project_id, run_id))

    # --- worker-side transitions (called by app.worker) ---
    def mark_running(self, run_id: str, project_id: str) -> None:
        run = self.get(run_id, project_id)
        if run:
            run.status = RunStatus.running

    def complete(self, run_id: str, project_id: str) -> None:
        run = self.get(run_id, project_id)
        if run:
            run.status = RunStatus.completed

    def fail(self, run_id: str, project_id: str) -> None:
        run = self.get(run_id, project_id)
        if run:
            run.status = RunStatus.failed


# Process-local singleton for the scaffold; swap for an RDS-backed store.
RUNS = RunStore()
