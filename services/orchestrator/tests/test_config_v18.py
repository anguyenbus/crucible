"""Config-evolution + two-hash determinism tests (NeMo Task Group 2): 1.8.0.

``legal-rag-default-1.8.0`` is the "nemo-all" config: every guard VERDICT routes
through the out-of-process NeMo pod, and the in-house DECISION gates are DISABLED
ON THIS CONFIG ONLY via two EMPTY category tuples (``input_categories`` /
``output_categories``). The in-house code stays present + unchanged for older
configs / rollback — it is merely UNWIRED here.

It carries EVERY 1.4.0 non-guardrail pin forward BYTE-IDENTICAL (through the
retired 1.5.0-1.7.0 stepping stones, which shared 1.4.0's non-guardrail pins) and
differs only in the guardrails block:

- ``input_categories`` / ``output_categories`` set EMPTY (in-house decision gates
  off);
- ``nemo.input_self_check: true`` (the pod's ``self_check_input`` replaces the
  in-house Haiku confirm-step), ``output_self_check: true``, ``check_facts: true``;
- a FRESH ``config_dir_digest`` (DIFFERENT from every retired pin) + a BUMPED pod
  ``config_version`` (1.5.0 — the Group 4 input-triage rail's ``config/`` edits:
  the ``input_triage`` prompt, the ``guarded input`` dispatcher, and
  ``rails/input_triage.co``); ``uv_lock_sha256`` carried forward UNCHANGED (no
  dependency was added).

Configs 1.0.0-1.4.0 are IMMUTABLE history: their bytes AND manifest entries must
stay byte-exact.

Focused checks only (per task 2.1) — exhaustive per-pin permutations skipped.
"""

import hashlib
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = SERVICE_ROOT / "app" / "configs"

# The FRESH config_dir_digest pinned into 1.8.0.yaml, covering the 2026-07-25
# ingest-time /check/chunks injection table (config/injections.yml). Regen + publish:
# `cd services/guardrail && uv run python -m app.config_digest --write-env`.
FRESH_CONFIG_DIR_DIGEST = "7db3d4b5f97243dc9c374fe2248ecc0961ad191f2030e119c5ca5f862411dc76"
# Retired digests — pinned here to PROVE 1.8.0 moved off each of them.
PRE_FOLD_CONFIG_DIR_DIGEST = "413182c3937f6345ed957fcb2bc2c3f166672f75352367b7ed7bdae51ce5ec89"
PRE_CALIBRATION_CONFIG_DIR_DIGEST = (
    "b5da30dbae57309c6045138bd569bf13d7c67ed19ceb2153484183e1eb7b2183"
)
# The post-calibration / pre-triage pin (the Group 3 default), superseded by the
# Group 4 input-triage config edits.
PRE_TRIAGE_CONFIG_DIR_DIGEST = "19e706bf05435f605bd3d04e2c37a79c69bcf5c681837aa201bce4e64f22a58d"
# The Group 4 input-triage pin, superseded by the ingest-time /check/chunks table.
PRE_CHUNKS_CONFIG_DIR_DIGEST = "338c2abcfa6cdd2f93c79c86ba0cab1f9c2f3397e99fb3ce2ea0f8beaa89a3f6"
# uv.lock is UNCHANGED by a config/ edit — the SAME value 1.4.0's pod lane pins.
UV_LOCK_SHA256 = "86d6e9a5b1b7b1bb4bd549747890c232e5d8d521ec5078767ad7d060b1f19603"

# 1.4.0 (the in-house lane) must stay byte-exact — the rollback fallback.
ORIGINAL_1_4_0_HASH = "9c9cc3d84d723a78ee00195e1285eb568bdf08afc6b7bd162ed1682b1024e2f9"


def test_1_8_0_disables_in_house_decision_gates_while_1_4_0_is_unchanged():
    """1.8.0 has EMPTY in-house input/output categories; 1.4.0 keeps its in-house gates."""
    from app.config import resolve_pipeline_config

    g18 = resolve_pipeline_config("legal-rag-default-1.8.0").config.guardrails
    g14 = resolve_pipeline_config("legal-rag-default-1.4.0").config.guardrails

    # In-house DECISION gates DISABLED on 1.8.0 (both empty) — pod owns verdicts.
    assert g18.input_categories == ()
    assert g18.output_categories == ()
    # But the in-house code stays WIRED-BUT-INERT: enabled + classifier are kept
    # (so rollback is a config-ref swap), just never fired (empty categories).
    assert g18.enabled is True
    assert g18.classifier_model_id == "au.anthropic.claude-haiku-4-5-20251001-v1:0"

    # 1.4.0 (rollback fallback) still carries its in-house decision gates intact.
    assert g14.input_categories == ("prompt_leak", "jailbreak", "unicode_evasion")
    assert g14.output_categories == ("pii", "secrets")
    assert g14.nemo is None  # 1.4.0 has NO NeMo lane at all


def test_1_8_0_routes_every_verdict_through_the_pod():
    """The nemo selector routes INPUT + OUTPUT (policy + facts) through the pod."""
    from app.config import resolve_pipeline_config
    from app.schemas.pipeline_config import NemoGuardPin

    nemo = resolve_pipeline_config("legal-rag-default-1.8.0").config.guardrails.nemo
    assert isinstance(nemo, NemoGuardPin)
    assert nemo.enabled is True
    # INPUT verdict via the pod (NEW on 1.8.0) — retires the in-house Haiku confirm.
    assert nemo.input_self_check is True
    # OUTPUT policy + grounding verdicts via the pod.
    assert nemo.output_self_check is True
    assert nemo.check_facts is True
    # BUMPED pod config label — the ingest-time /check/chunks injection table
    # (config/injections.yml) is the next config/ edit after the Group 4 triage
    # rail: config_version 1.5.0 -> 1.6.0.
    assert nemo.config_version == "1.6.0"


def test_1_8_0_carries_1_4_0_non_guard_pins_forward_byte_identical():
    """Every non-guardrail pin equals 1.4.0's (generator/embedder/template/...)."""
    from app.config import resolve_pipeline_config

    c18 = resolve_pipeline_config("legal-rag-default-1.8.0").config
    c14 = resolve_pipeline_config("legal-rag-default-1.4.0").config
    assert c18.region == c14.region
    assert c18.generator == c14.generator
    assert c18.embedder == c14.embedder
    assert c18.prompt_template == c14.prompt_template
    assert c18.retrieval == c14.retrieval
    assert c18.context == c14.context
    assert c18.history == c14.history


def test_1_8_0_pins_a_fresh_digest_that_moved_off_every_retired_pin():
    """1.8.0's config_dir_digest is the FRESH value, not any retired one."""
    from app.config import resolve_pipeline_config

    nemo = resolve_pipeline_config("legal-rag-default-1.8.0").config.guardrails.nemo
    assert nemo.config_dir_digest == FRESH_CONFIG_DIR_DIGEST
    assert nemo.config_dir_digest not in (
        PRE_FOLD_CONFIG_DIR_DIGEST,
        PRE_CALIBRATION_CONFIG_DIR_DIGEST,
        PRE_TRIAGE_CONFIG_DIR_DIGEST,
        PRE_CHUNKS_CONFIG_DIR_DIGEST,
    )
    # uv.lock carried forward (a config/ edit does not touch it).
    assert nemo.uv_lock_sha256 == UV_LOCK_SHA256


def test_1_8_0_config_sha256_matches_manifest_and_both_hashes_participate():
    """config_sha256 truncates the raw-byte hash; both determinism hashes are IN those bytes."""
    from app.config import CONFIG_SHA256_LENGTH, load_config_manifest, resolve_pipeline_config

    raw = (CONFIGS_DIR / "legal-rag-default-1.8.0.yaml").read_bytes()
    full_hash = hashlib.sha256(raw).hexdigest()
    assert load_config_manifest()["legal-rag-default-1.8.0.yaml"] == full_hash

    resolved = resolve_pipeline_config("legal-rag-default-1.8.0")
    assert resolved.config_sha256 == full_hash[:CONFIG_SHA256_LENGTH]
    # A pod-config OR uv.lock change (which would change these literal strings)
    # forces a different 1.8.0 config_sha256 — the digest-enforcement seam.
    assert FRESH_CONFIG_DIR_DIGEST.encode() in raw
    assert UV_LOCK_SHA256.encode() in raw


def test_1_0_through_1_4_untouched_and_manifest_covers_the_whole_set():
    """BINDING: 1.4.0 stays byte-exact; the full packaged set matches the manifest."""
    from app.config import load_config_manifest

    manifest = load_config_manifest()
    raw_140 = (CONFIGS_DIR / "legal-rag-default-1.4.0.yaml").read_bytes()
    assert hashlib.sha256(raw_140).hexdigest() == ORIGINAL_1_4_0_HASH
    assert manifest["legal-rag-default-1.4.0.yaml"] == ORIGINAL_1_4_0_HASH

    recomputed = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(CONFIGS_DIR.glob("*.yaml"))
    }
    assert recomputed == manifest
    assert "legal-rag-default-1.8.0.yaml" in manifest
