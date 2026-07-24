"""Group 3: flip the product default to the guarded config (LLM lane shadow).

Today the default config is UNGUARDED, so a shadow verdict observes nothing. The
flip is what makes shadow data real — but only with the LLM lane in SHADOW (the
Group 2 mode flag defaults every LLM class to shadow) and the deterministic
floor ENFORCING (Group 1, pod-independent, never governed by the mode flag).

Non-negotiable regression guard: eval's `1.1.0` MEASUREMENT lane is FROZEN — it
is pinned INDEPENDENTLY in the eval service's own adapter, so flipping the
orchestrator product default must not touch it. That do-not-touch is asserted
here explicitly.
"""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_PIPELINE_CONFIG_REF, load_config_manifest, resolve_pipeline_config
from app.orchestrator import guardrails
from app.orchestrator.guard_policy import GuardModes, VerdictClass

_GUARDED_REF = "legal-rag-default-1.8.0"
_REPO_ORCH = Path(__file__).resolve().parents[1]
_REPO_SERVICES = Path(__file__).resolve().parents[2]

# The committed 1.1.0 file hash (test_config_v11/v12 pin the SAME value): the
# eval measurement lane's config bytes must stay byte-identical.
_FROZEN_1_1_0_SHA256 = "6bd159eacb78bee2ff79c8216a6fc10ef68cc9adff16c7f53cec18d830f7c624"


def test_product_default_is_the_guarded_config_pod_owns_every_verdict():
    assert DEFAULT_PIPELINE_CONFIG_REF == _GUARDED_REF
    pins = resolve_pipeline_config(_GUARDED_REF).config.guardrails
    # The pod owns every verdict (nemo-all): input + output self-check on.
    assert pins.enabled is True
    assert pins.nemo is not None
    assert pins.nemo.enabled is True
    assert pins.nemo.input_self_check is True
    assert pins.nemo.output_self_check is True
    # The deterministic floor (Group 1) is ACTIVE on the guarded default.
    assert guardrails.input_floor_active(pins) is True


def test_llm_lane_defaults_to_shadow_deterministic_floor_not_governed():
    """The NEW LLM layer ships in SHADOW; the deterministic floor stays enforcing."""
    modes = GuardModes()
    assert not modes.is_enforcing(VerdictClass.ATTACK)
    assert not modes.is_enforcing(VerdictClass.OFFTOPIC)
    assert not modes.is_enforcing(VerdictClass.OUTPUT)
    # The deterministic floor is not a flippable LLM class — always enforcing.
    assert not hasattr(VerdictClass, "MALFORMED")
    pins = resolve_pipeline_config(_GUARDED_REF).config.guardrails
    assert guardrails.input_floor_active(pins) is True


def test_chainlit_demo_pin_is_flipped_to_the_guarded_config():
    src = (_REPO_ORCH / "demo_ui" / "chat_ui" / "config.py").read_text(encoding="utf-8")
    assert 'DEFAULT_PIPELINE_CONFIG = "legal-rag-default-1.8.0"' in src
    assert 'DEFAULT_PIPELINE_CONFIG = "legal-rag-default-1.4.0"' not in src


def test_guarded_default_carries_two_hash_determinism_pins():
    """Both determinism hashes are pinned (they enter config_sha256 → drift re-pins)."""
    resolved = resolve_pipeline_config(_GUARDED_REF)
    nemo = resolved.config.guardrails.nemo
    assert nemo is not None
    assert nemo.config_dir_digest is not None and len(nemo.config_dir_digest) == 64
    assert nemo.uv_lock_sha256 is not None and len(nemo.uv_lock_sha256) == 64
    # Both are bytes of the config file, so they ride into config_sha256: a
    # drifted config/ or uv.lock forces a DIFFERENT pin hash (pod refuses to
    # serve on live-digest mismatch — services/guardrail verify_pinned_digests).
    raw = (_REPO_ORCH / "app" / "configs" / f"{_GUARDED_REF}.yaml").read_bytes()
    assert nemo.config_dir_digest.encode() in raw
    assert nemo.uv_lock_sha256.encode() in raw
    assert resolved.config_sha256  # non-empty truncated content hash


def test_eval_1_1_0_measurement_lane_is_frozen():
    """DO-NOT-TOUCH: the eval lane is pinned independently in the eval adapter."""
    # The eval service's OWN default (never the orchestrator constant) stays 1.1.0.
    eval_stub = _REPO_SERVICES / "eval" / "dev" / "stubs" / "rag" / "orchestrator_query.py"
    src = eval_stub.read_text(encoding="utf-8")
    assert 'DEFAULT_PIPELINE_CONFIG: Final[str] = "legal-rag-default-1.1.0"' in src
    # The 1.1.0 config bytes are unchanged (manifest hash frozen).
    assert load_config_manifest()["legal-rag-default-1.1.0.yaml"] == _FROZEN_1_1_0_SHA256
