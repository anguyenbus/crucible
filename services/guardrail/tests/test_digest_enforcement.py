"""
Demo-run digest enforcement for the nemo-all ``legal-rag-default-1.8.0`` pin.

``test_config_digest.py`` proves ``verify_pinned_digests`` is correct against
FRESHLY-computed values; THIS file tracks the LITERAL 64-hex constants the demo
``guardrail-pod`` Makefile target injects as
``GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST`` / ``GUARDRAIL_EXPECTED_UV_LOCK_SHA256``.

Task Group 1 folded ``config/detectors.yml`` into ``config_dir_digest`` (one
determinism mechanism, no new pin hash). That intentionally changed the live pod
config digest, so the PRE-FOLD demo config-dir pin was SUPERSEDED. Task Group 2
minted the FRESH post-fold digest into the active nemo config
``legal-rag-default-1.8.0`` and rewired the demo Makefile to it. This file now
asserts the RECONCILED truth:

- the FRESH ``1.8.0`` demo config-dir digest MATCHES live (the pod serves against
  its own post-fold config/),
- the PRE-FOLD config-dir digest STILL no longer matches live (refuse-on-mismatch
  — proving the fold moved the digest),
- ``uv.lock`` is untouched by the fold, so its sha still matches.

No AWS, no live pod.
"""

from __future__ import annotations

import pytest
from app.config_digest import (
    DigestMismatchError,
    compute_config_dir_digest,
    compute_lock_sha256,
    default_config_dir,
    default_lock_path,
    verify_pinned_digests,
)

# The FRESH post-fold literal demo config-dir digest — the ``1.8.0`` pin the demo
# Makefile injects as GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST. Regen:
#   cd services/guardrail && uv run python -m app.config_digest
DEMO_EXPECTED_CONFIG_DIR_DIGEST = (
    "b5da30dbae57309c6045138bd569bf13d7c67ed19ceb2153484183e1eb7b2183"
)
# The PRE-FOLD literal demo config-dir digest (the retired pre-fold pin),
# retained to PROVE the detectors.yml fold moved the live digest off it.
PRE_FOLD_CONFIG_DIR_DIGEST = (
    "413182c3937f6345ed957fcb2bc2c3f166672f75352367b7ed7bdae51ce5ec89"
)
# uv.lock is NOT touched by a config/ fold, so its sha is stable across the fold.
DEMO_EXPECTED_UV_LOCK_SHA256 = (
    "86d6e9a5b1b7b1bb4bd549747890c232e5d8d521ec5078767ad7d060b1f19603"
)


def test_the_1_8_0_demo_config_digest_matches_live_and_serves():
    """The RECONCILED 1.8.0 demo config-dir pin matches the post-fold live config/."""
    assert compute_config_dir_digest(default_config_dir()) == DEMO_EXPECTED_CONFIG_DIR_DIGEST
    # The pod serves against its own config with the 1.8.0 pins enforced.
    verify_pinned_digests(
        config_dir=default_config_dir(),
        lock_path=default_lock_path(),
        expected_config_dir_digest=DEMO_EXPECTED_CONFIG_DIR_DIGEST,
        expected_uv_lock_sha256=DEMO_EXPECTED_UV_LOCK_SHA256,
    )


def test_pre_fold_config_digest_is_still_superseded_by_the_detectors_fold():
    """The PRE-FOLD config-dir pin no longer matches live → refuse."""
    live = compute_config_dir_digest(default_config_dir())
    assert live != PRE_FOLD_CONFIG_DIR_DIGEST, (
        "Expected the detectors.yml fold to change the live config_dir_digest; if "
        "these are equal the fold did not take."
    )
    with pytest.raises(DigestMismatchError, match="config_dir_digest"):
        verify_pinned_digests(
            config_dir=default_config_dir(),
            lock_path=default_lock_path(),
            expected_config_dir_digest=PRE_FOLD_CONFIG_DIR_DIGEST,
            expected_uv_lock_sha256=DEMO_EXPECTED_UV_LOCK_SHA256,
        )


def test_uv_lock_sha_is_unaffected_by_the_fold():
    """The fold touches only config/, so the recorded uv.lock sha still matches live."""
    assert compute_lock_sha256(default_lock_path()) == DEMO_EXPECTED_UV_LOCK_SHA256


def test_a_wrong_expected_digest_refuses_to_serve():
    """A wrong expected config-dir digest → DigestMismatchError (refuse-on-mismatch)."""
    with pytest.raises(DigestMismatchError, match="config_dir_digest"):
        verify_pinned_digests(
            config_dir=default_config_dir(),
            lock_path=default_lock_path(),
            expected_config_dir_digest="f" * 64,
            expected_uv_lock_sha256=DEMO_EXPECTED_UV_LOCK_SHA256,
        )
