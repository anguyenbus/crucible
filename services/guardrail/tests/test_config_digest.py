"""
Two-hash config determinism (Phase 4 / I4): digest computation, tamper-evidence,
and pod startup self-verification.

The pod computes — in ``services/guardrail/`` — the two values folded into the
orchestrator's ACTIVE NeMo pin's ``nemo`` selector: the NeMo ``config/``
directory digest and the ``uv.lock`` sha256. These tests prove the digest is
deterministic and order-independent, that the pod REFUSES to serve on a digest
mismatch (a drifted image cannot answer under a pinned config), and that
``detectors.yml`` is now a hashed member of the SAME ``config_dir_digest``
(Task Group 1 — one determinism mechanism, no new pin hash).

Task Group 1 fold: ``config/detectors.yml`` (the deterministic secrets/PII
pattern tables) is now a MANDATORY member of ``config_dir_digest``. This changed
the live pod-config digest, so any PRE-FOLD config-dir pin (authored BEFORE the
fold) no longer matches live — that is the intended effect (a regex change
becomes a re-pin). Task Group 2 minted the fresh post-fold digest into the active
nemo config ``legal-rag-default-1.8.0`` and retargeted the demo digest-enforcement
audit link to it (see ``test_digest_enforcement.py``).

No AWS, no live pod.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config_digest import (
    DigestMismatchError,
    compute_config_dir_digest,
    compute_lock_sha256,
    default_config_dir,
    default_lock_path,
    verify_pinned_digests,
)


def _write_min_config(root: Path) -> None:
    """Write the three mandatory digest members into ``root`` (config.yml + prompts.yml + detectors.yml)."""
    (root / "config.yml").write_text("models: []\n")
    (root / "prompts.yml").write_text("prompts: []\n")
    (root / "detectors.yml").write_text("secrets: []\n")


def test_config_dir_digest_is_deterministic_and_64_hex():
    """Same config/ ⇒ same digest, stable across repeated computation."""
    config_dir = default_config_dir()
    first = compute_config_dir_digest(config_dir)
    second = compute_config_dir_digest(config_dir)
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_config_dir_digest_changes_when_a_config_byte_changes(tmp_path):
    """A one-byte edit to any member file yields a different digest (tamper-evident)."""
    _write_min_config(tmp_path)
    baseline = compute_config_dir_digest(tmp_path)

    (tmp_path / "prompts.yml").write_text("prompts: []\n# drift\n")
    assert compute_config_dir_digest(tmp_path) != baseline


def test_config_dir_digest_changes_when_detectors_change(tmp_path):
    """detectors.yml is a hashed member: a detector edit forces a fresh digest (Task Group 1)."""
    _write_min_config(tmp_path)
    baseline = compute_config_dir_digest(tmp_path)

    # A regex/detector change (e.g. adding a pattern) must move the digest so it
    # becomes a pod config_version bump + fresh orchestrator config_sha256.
    (tmp_path / "detectors.yml").write_text("secrets: []\n# new rule\n")
    assert compute_config_dir_digest(tmp_path) != baseline


def test_config_dir_digest_is_order_independent_over_rails(tmp_path):
    """rails/*.co members enter in sorted order — enumeration order can't change it."""
    _write_min_config(tmp_path)
    rails = tmp_path / "rails"
    rails.mkdir()
    (rails / "b.co").write_text("define flow b\n")
    (rails / "a.co").write_text("define flow a\n")
    first = compute_config_dir_digest(tmp_path)
    # Recompute after touching mtimes in a different order — sorted-by-relpath
    # keeps the digest stable.
    (rails / "b.co").write_text("define flow b\n")
    (rails / "a.co").write_text("define flow a\n")
    assert compute_config_dir_digest(tmp_path) == first


def test_verify_pinned_digests_passes_on_a_match():
    """Matching expectations ⇒ no raise (the pod would serve)."""
    live_config = compute_config_dir_digest(default_config_dir())
    live_lock = compute_lock_sha256(default_lock_path())
    verify_pinned_digests(
        config_dir=default_config_dir(),
        lock_path=default_lock_path(),
        expected_config_dir_digest=live_config,
        expected_uv_lock_sha256=live_lock,
    )
    # A None expectation is skipped (dev runs with no pin still serve).
    verify_pinned_digests(
        config_dir=default_config_dir(),
        lock_path=default_lock_path(),
        expected_config_dir_digest=None,
        expected_uv_lock_sha256=None,
    )


def test_verify_pinned_digests_raises_on_a_mismatch():
    """A drifted config/ or uv.lock ⇒ DigestMismatchError (pod refuses to serve)."""
    with pytest.raises(DigestMismatchError, match="config_dir_digest"):
        verify_pinned_digests(
            config_dir=default_config_dir(),
            lock_path=default_lock_path(),
            expected_config_dir_digest="0" * 64,
            expected_uv_lock_sha256=None,
        )
    with pytest.raises(DigestMismatchError, match="uv_lock_sha256"):
        verify_pinned_digests(
            config_dir=default_config_dir(),
            lock_path=default_lock_path(),
            expected_config_dir_digest=None,
            expected_uv_lock_sha256="0" * 64,
        )


def test_missing_detectors_yml_is_a_hard_digest_error(tmp_path):
    """detectors.yml is MANDATORY: its absence is a FileNotFoundError, not a silent skip."""
    (tmp_path / "config.yml").write_text("models: []\n")
    (tmp_path / "prompts.yml").write_text("prompts: []\n")
    with pytest.raises(FileNotFoundError, match="detectors.yml"):
        compute_config_dir_digest(tmp_path)


def test_pod_lifespan_refuses_to_serve_on_digest_mismatch(monkeypatch):
    """End-to-end: a mismatched pinned digest aborts pod startup (no serve)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.settings import Settings

    # A settings snapshot pinned to a WRONG config-dir digest, with rails
    # pre-installed so lifespan never tries to build a real engine.
    app.state.settings = Settings(
        model_id="au.anthropic.claude-haiku-4-5-20251001-v1:0",
        region="ap-southeast-2",
        config_dir=str(default_config_dir()),
        lock_path=str(default_lock_path()),
        expected_config_dir_digest="0" * 64,
        expected_uv_lock_sha256=None,
    )
    app.state.rails = object()
    try:
        with pytest.raises(DigestMismatchError):
            with TestClient(app):
                pass  # entering the context runs lifespan → verification raises
    finally:
        for attr in ("settings", "rails", "detectors", "detectors_error"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)
