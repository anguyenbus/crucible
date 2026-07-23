"""
The SINGLE cross-service digest ripple for the 2026-07-23 calibration slice.

Two ``config/`` edits landed — the ``pii:`` detector-table withdrawal (item 6)
and the ``self_check_output`` advice boundary (item 7) — and both are members of
``config_dir_digest``. They therefore share **ONE** regeneration, **ONE** re-pin
into ``legal-rag-default-1.8.0``, and **ONE** ``nemo.config_version`` bump
(``1.3.0`` → ``1.4.0``). This file is what stops that ripple being forgotten or
applied twice: a forgotten re-pin leaves a pod that refuses to serve the moment
the determinism pins are populated in a real deploy surface.

Two invariants are asserted hard here:

* ``uv_lock_sha256`` MUST NOT MOVE. This slice adds no dependency, so a moved
  lock hash is a review failure, not a merge conflict to resolve. The literal
  pre-slice value is asserted directly.
* Configs ``1.0.0``–``1.7.0`` are IMMUTABLE released history and are untouched;
  only the not-yet-cut-over ``1.8.0`` is edited (see its header for the
  mutate-in-place judgement call).

The digest lives on the guardrail side because only this service can compute it
(``app.config_digest``); the pin lives on the orchestrator side. No AWS, no live
pod, no Phoenix.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from app.config_digest import (
    compute_config_dir_digest,
    compute_lock_sha256,
    default_config_dir,
    default_lock_path,
    default_pin_env_path,
    read_pin_env,
)

_ORCHESTRATOR_CONFIGS = (
    Path(__file__).resolve().parents[2] / "orchestrator" / "app" / "configs"
)
_CONFIG_1_8_0 = _ORCHESTRATOR_CONFIGS / "legal-rag-default-1.8.0.yaml"
_MANIFEST = _ORCHESTRATOR_CONFIGS / "manifest.sha256"

# The PRE-SLICE uv.lock sha256. Asserted as a LITERAL, not recomputed-and-compared:
# the point of the bar is that this value did not move, and a recomputed
# comparison would happily follow a moved lock.
PRE_SLICE_UV_LOCK_SHA256 = "86d6e9a5b1b7b1bb4bd549747890c232e5d8d521ec5078767ad7d060b1f19603"

# Released, immutable configs and their PRE-SLICE manifest hashes. 1.5.0-1.7.0 were
# retired stepping stones and were never packaged, so the released set is 1.0.0-1.4.0.
IMMUTABLE_CONFIG_HASHES = {
    "legal-rag-default-1.0.0.yaml": "0a3376d2e7cd9fc95c26f4681b7363e29dc6c892bb430c4645980e8890609950",
    "legal-rag-default-1.1.0.yaml": "6bd159eacb78bee2ff79c8216a6fc10ef68cc9adff16c7f53cec18d830f7c624",
    "legal-rag-default-1.2.0.yaml": "ac690a053332847d41a016ddbec2703d93973c71ddc0419c31ece8c11730b6dc",
    "legal-rag-default-1.3.0.yaml": "0249750b5064823da6f7fbf57924681b8cdeb3b6ff67e28a05cf529f90bb0cfd",
    "legal-rag-default-1.4.0.yaml": "9c9cc3d84d723a78ee00195e1285eb568bdf08afc6b7bd162ed1682b1024e2f9",
}


def _nemo_pin() -> dict:
    """The ``guardrails.nemo`` selector block of the active 1.8.0 config."""
    doc = yaml.safe_load(_CONFIG_1_8_0.read_text(encoding="utf-8"))
    return doc["guardrails"]["nemo"]


def _manifest() -> dict[str, str]:
    """``filename -> sha256`` parsed from the packaged config manifest."""
    entries: dict[str, str] = {}
    for line in _MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        entries[name.strip()] = digest.strip()
    return entries


def test_the_recomputed_digest_equals_the_value_pinned_in_1_8_0():
    """ONE fresh digest, covering BOTH config/ edits, is what 1.8.0 pins."""
    live = compute_config_dir_digest(default_config_dir())

    assert _nemo_pin()["config_dir_digest"] == live
    # And the same value is published as the single generated deploy source, so
    # no deploy surface can drift from the pin.
    pins = read_pin_env(default_pin_env_path())
    assert pins["GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST"] == live


def test_the_pod_config_version_tracks_the_config_dir_edits():
    """Each `config/` edit bumps the pod config_version exactly once."""
    assert _nemo_pin()["config_version"] == "1.4.0"


def test_uv_lock_sha256_did_not_move():
    """This slice adds no dependency: the lock hash is byte-identical, live and pinned."""
    assert compute_lock_sha256(default_lock_path()) == PRE_SLICE_UV_LOCK_SHA256
    assert _nemo_pin()["uv_lock_sha256"] == PRE_SLICE_UV_LOCK_SHA256
    pins = read_pin_env(default_pin_env_path())
    assert pins["GUARDRAIL_EXPECTED_UV_LOCK_SHA256"] == PRE_SLICE_UV_LOCK_SHA256


def test_manifest_verifies_for_1_8_0():
    """manifest.sha256 was regenerated: its 1.8.0 entry matches the file bytes."""
    actual = hashlib.sha256(_CONFIG_1_8_0.read_bytes()).hexdigest()
    assert _manifest()["legal-rag-default-1.8.0.yaml"] == actual


@pytest.mark.parametrize("name,expected", sorted(IMMUTABLE_CONFIG_HASHES.items()))
def test_released_configs_1_0_0_through_1_7_0_are_untouched(name, expected):
    """Released history stays byte-exact in BOTH the file and the manifest."""
    raw = (_ORCHESTRATOR_CONFIGS / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected
    assert _manifest()[name] == expected


def test_the_manifest_covers_exactly_the_packaged_config_set():
    """No config file is missing from, or stale in, the regenerated manifest."""
    recomputed = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_ORCHESTRATOR_CONFIGS.glob("*.yaml"))
    }
    assert recomputed == _manifest()
