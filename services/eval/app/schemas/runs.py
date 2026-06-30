"""Run-domain Pydantic models — the subset needed to submit and read runs.

These mirror eval-openapi.yaml (the hand-authored contract). Once the FastAPI
app is the source of truth, `scripts/export-openapi.py` would emit the spec FROM
these models and CI would diff it against the committed contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TargetType(str, Enum):
    dataset = "dataset"
    model = "model"
    parse_run = "parse_run"


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    cancelling = "cancelling"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class InitiatedBy(str, Enum):
    schedule = "schedule"
    api = "api"


class Target(BaseModel):
    """Discriminated on `type`; extra source/model/parser fields are preserved."""

    model_config = ConfigDict(extra="allow")
    type: TargetType


class EvalRunRequest(BaseModel):
    target: Target
    config_id: str
    name: str | None = Field(default=None, max_length=200)
    dataset_id: str | None = None
    metrics: list[str] | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    labels: dict[str, str] | None = None


class ResultCounts(BaseModel):
    total: int = 0
    scored: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0


class EvalRun(BaseModel):
    run_id: str
    status: RunStatus
    initiated_by: InitiatedBy
    target: Target
    config_id: str
    dataset_id: str | None = None
    repro_key: str
    result_counts: ResultCounts = Field(default_factory=ResultCounts)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    links: dict[str, str] = Field(default_factory=dict)
