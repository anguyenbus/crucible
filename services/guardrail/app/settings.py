"""
Pod settings resolved from the ambient environment (no I/O at import).

Mirrors the orchestrator's construct-in-lifespan discipline: importing this
module reads NOTHING; :func:`get_settings` is called ONCE in the FastAPI
lifespan. Every value has a Bedrock-Haiku default matching the Phase-0 spike so
the pod is runnable with zero configuration, and each is override-able by env
for the independent-K8s-pod model (compose is dev-only, not the only wiring).

The Haiku model id is stamped into every response by the pod itself — NeMo's
own ``LLMCallInfo.llm_model_name`` came back ``"unknown"`` in the spike, so the
pod NEVER trusts NeMo for the id (Phase-0 FINDINGS #2).

Phase 4 (two-hash determinism / I4): the pod also carries the config/ directory
and the resolved ``uv.lock`` locations plus the EXPECTED determinism hashes it
was pinned to answer under (the active nemo config pin, ``legal-rag-default-1.8.0``'s
``config_dir_digest`` / ``uv_lock_sha256``). The expected values are injected by env
(``GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST`` / ``GUARDRAIL_EXPECTED_UV_LOCK_SHA256``,
CI/deploy-populated from the active nemo pin); when either is set the lifespan
self-verification computes the LIVE digests and refuses to serve on mismatch, so
a drifted image cannot silently answer under a stale pin.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Phase-0 resolved facts (services/guardrail/spike/FINDINGS.md): Haiku on the
# au.* inference profile, ap-southeast-2, temperature 0. These are DEFAULTS —
# the shipping config/ (Phase 2) pins them in config.yml; the pod stamps this id
# into the contract because NeMo's reported id is unreliable.
_DEFAULT_MODEL_ID: Final[str] = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
_DEFAULT_REGION: Final[str] = "ap-southeast-2"

# The shipping NeMo config/ lives at the package root (Phase 2 authors its
# config.yml + prompts.yml + rails/*.co); resolve it relative to this file so
# the pod finds it whether launched from the repo or the container WORKDIR.
_DEFAULT_CONFIG_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "config"
# The resolved uv.lock (the locked dependency artifact) sits at the package
# root, the config/ sibling.
_DEFAULT_LOCK_PATH: Final[Path] = Path(__file__).resolve().parent.parent / "uv.lock"


@dataclass(frozen=True)
class Settings:
    """Immutable pod settings (plain data; no telemetry / AWS types)."""

    model_id: str
    region: str
    config_dir: str
    # Location of the resolved uv.lock, for the two-hash determinism check.
    lock_path: str = str(_DEFAULT_LOCK_PATH)
    # Expected determinism hashes the pod was pinned to answer under (the
    # active nemo config pin's values). None ⇒ that half of the self-verification
    # is skipped (dev runs with no pinned expectation still serve).
    expected_config_dir_digest: str | None = None
    expected_uv_lock_sha256: str | None = None


def get_settings() -> Settings:
    """
    Resolve pod settings from the environment ONCE (called in lifespan).

    Env overrides (all optional): ``GUARDRAIL_MODEL_ID``, ``GUARDRAIL_REGION``,
    ``GUARDRAIL_CONFIG_DIR``, ``GUARDRAIL_LOCK_PATH``,
    ``GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST``, ``GUARDRAIL_EXPECTED_UV_LOCK_SHA256``.
    Credentials themselves come from the ambient AWS credential chain (instance
    role) — never read here, never hardcoded.
    """
    return Settings(
        model_id=os.environ.get("GUARDRAIL_MODEL_ID", _DEFAULT_MODEL_ID),
        region=os.environ.get("GUARDRAIL_REGION", _DEFAULT_REGION),
        config_dir=os.environ.get("GUARDRAIL_CONFIG_DIR", str(_DEFAULT_CONFIG_DIR)),
        lock_path=os.environ.get("GUARDRAIL_LOCK_PATH", str(_DEFAULT_LOCK_PATH)),
        expected_config_dir_digest=os.environ.get("GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST"),
        expected_uv_lock_sha256=os.environ.get("GUARDRAIL_EXPECTED_UV_LOCK_SHA256"),
    )
