"""
Demo-run digest enforcement for the nemo-all ``legal-rag-default-1.8.0`` pin.

``test_config_digest.py`` proves ``verify_pinned_digests`` is correct against
FRESHLY-computed values; THIS file tracks the LITERAL 64-hex constants the demo
``guardrail-pod`` Makefile target injects as
``GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST`` / ``GUARDRAIL_EXPECTED_UV_LOCK_SHA256``.

Every ``config/`` edit SUPERSEDES the previous config-dir pin. Three have landed:
the ``detectors.yml`` fold, the 2026-07-23 guardrail-calibration slice (the
``pii:`` withdrawal + the ``self_check_output`` advice boundary), and the
2026-07-24 Group 4 input-triage rail (the ``input_triage`` prompt + the
``guarded input`` dispatcher + ``rails/input_triage.co`` — ONE digest bump, pod
``config_version`` 1.4.0 → 1.5.0). This file asserts the RECONCILED truth:

- the CURRENT ``1.8.0`` config-dir digest MATCHES live (the pod serves against
  its own config/),
- every SUPERSEDED digest still fails to match live (refuse-on-mismatch —
  proving each edit genuinely moved the digest),
- ``uv.lock`` is untouched by a ``config/`` edit, so its sha still matches, and
- the literals here agree with the SINGLE GENERATED pin source
  (``deploy/guardrail-pins.env``) that every deploy surface reads — a drift
  between the two is how a stale hand-copied pin reaches a running pod.

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
    default_pin_env_path,
    read_pin_env,
    verify_pinned_digests,
)

# The CURRENT literal config-dir digest — the ``1.8.0`` pin, and the value the
# demo Makefile injects as GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST. Regen:
#   cd services/guardrail && uv run python -m app.config_digest --write-env
DEMO_EXPECTED_CONFIG_DIR_DIGEST = (
    "7db3d4b5f97243dc9c374fe2248ecc0961ad191f2030e119c5ca5f862411dc76"
)
# Retired config-dir digests, retained to PROVE each config/ edit moved the live
# digest off the previous pin: the PRE-FOLD pin, the pre-calibration (post-fold)
# pin, then the pre-triage (post-calibration) pin superseded by the Group 4
# input-triage rail.
SUPERSEDED_CONFIG_DIR_DIGESTS = (
    "413182c3937f6345ed957fcb2bc2c3f166672f75352367b7ed7bdae51ce5ec89",
    "b5da30dbae57309c6045138bd569bf13d7c67ed19ceb2153484183e1eb7b2183",
    "19e706bf05435f605bd3d04e2c37a79c69bcf5c681837aa201bce4e64f22a58d",
    # The Group 4 input-triage pin, superseded by the ingest-time injections.yml
    # table (/check/chunks, config_version 1.5.0 -> 1.6.0).
    "338c2abcfa6cdd2f93c79c86ba0cab1f9c2f3397e99fb3ce2ea0f8beaa89a3f6",
)
# uv.lock is NOT touched by a config/ edit, so its sha is stable across all of them.
DEMO_EXPECTED_UV_LOCK_SHA256 = (
    "86d6e9a5b1b7b1bb4bd549747890c232e5d8d521ec5078767ad7d060b1f19603"
)


def test_the_1_8_0_demo_config_digest_matches_live_and_serves():
    """The RECONCILED 1.8.0 config-dir pin matches the live config/."""
    assert compute_config_dir_digest(default_config_dir()) == DEMO_EXPECTED_CONFIG_DIR_DIGEST
    # The pod serves against its own config with the 1.8.0 pins enforced.
    verify_pinned_digests(
        config_dir=default_config_dir(),
        lock_path=default_lock_path(),
        expected_config_dir_digest=DEMO_EXPECTED_CONFIG_DIR_DIGEST,
        expected_uv_lock_sha256=DEMO_EXPECTED_UV_LOCK_SHA256,
    )


@pytest.mark.parametrize("superseded", SUPERSEDED_CONFIG_DIR_DIGESTS)
def test_every_superseded_config_digest_still_refuses_to_serve(superseded):
    """Each retired config-dir pin no longer matches live → refuse."""
    live = compute_config_dir_digest(default_config_dir())
    assert live != superseded, (
        "Expected this config/ edit to change the live config_dir_digest; if "
        "these are equal the edit did not take."
    )
    with pytest.raises(DigestMismatchError, match="config_dir_digest"):
        verify_pinned_digests(
            config_dir=default_config_dir(),
            lock_path=default_lock_path(),
            expected_config_dir_digest=superseded,
            expected_uv_lock_sha256=DEMO_EXPECTED_UV_LOCK_SHA256,
        )


def test_uv_lock_sha_is_unaffected_by_config_edits():
    """A config/ edit touches no dependency, so the recorded uv.lock sha still matches."""
    assert compute_lock_sha256(default_lock_path()) == DEMO_EXPECTED_UV_LOCK_SHA256


def test_the_generated_pin_source_agrees_with_these_literals():
    """The deploy surface's single generated source carries exactly these pins."""
    pins = read_pin_env(default_pin_env_path())
    assert pins["GUARDRAIL_EXPECTED_CONFIG_DIR_DIGEST"] == DEMO_EXPECTED_CONFIG_DIR_DIGEST
    assert pins["GUARDRAIL_EXPECTED_UV_LOCK_SHA256"] == DEMO_EXPECTED_UV_LOCK_SHA256


def test_a_wrong_expected_digest_refuses_to_serve():
    """A wrong expected config-dir digest → DigestMismatchError (refuse-on-mismatch)."""
    with pytest.raises(DigestMismatchError, match="config_dir_digest"):
        verify_pinned_digests(
            config_dir=default_config_dir(),
            lock_path=default_lock_path(),
            expected_config_dir_digest="f" * 64,
            expected_uv_lock_sha256=DEMO_EXPECTED_UV_LOCK_SHA256,
        )
