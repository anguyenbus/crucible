"""Run endpoints — validate -> persist -> dispatch -> 202; poll via GET.

Control plane: imports schemas + the store + the dispatch shim. It must NOT
import app.runners / app.deepeval / app.phoenix (the scoring stack). The worker
does the scoring, out of band.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, status

from app.clients.dispatch import launch_run
from app.clients.store import RUNS
from app.schemas import EvalRun, EvalRunRequest

runs_router = APIRouter(prefix="/v1/eval", tags=["runs"])


def _project_id(x_project_id: str | None) -> str:
    # SCAFFOLD: real impl reads this from mTLS-context middleware (never the body).
    # Defaulted here so the endpoint is exercisable without the auth middleware.
    return x_project_id or "proj_local"


@runs_router.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=EvalRun)
def submit_run(
    body: EvalRunRequest,
    x_project_id: str | None = Header(default=None),
) -> EvalRun:
    project_id = _project_id(x_project_id)
    run = RUNS.persist(body, project_id)   # 1+2: validate (Pydantic) + write 'queued'
    launch_run(run.run_id)                  # 3: hand off to a worker (non-blocking)
    return run                              # 4: 202 + run_id; caller polls GET below


@runs_router.get("/runs/{run_id}", response_model=EvalRun)
def get_run(
    run_id: str,
    x_project_id: str | None = Header(default=None),
) -> EvalRun:
    run = RUNS.get(run_id, _project_id(x_project_id))
    if run is None:
        # 404 for unknown id AND cross-project (no existence leakage).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run
