"""Config-evolution tests (chainlit-chat-ui Task Group 6): history pins + 1.2.0.

The 1.0.0→1.1.0 precedent reused verbatim: new ``history`` pins get schema
DEFAULTS for backward RESOLUTION only; released YAMLs are never edited; the
new ``legal-rag-default-1.2.0`` pins EVERY value explicitly with the
history-aware template inlined under ``config_sha256``.

Focused checks only (per task 6.1) — exhaustive per-pin permutations are
intentionally skipped.
"""

import hashlib
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = SERVICE_ROOT / "app" / "configs"

# Hashes committed with the ORIGINAL releases — pinned here so any retroactive
# edit of an immutable artifact (or its manifest entry) fails this test even
# if both were changed together.
ORIGINAL_1_0_0_SHA256 = "0a3376d2e7cd9fc95c26f4681b7363e29dc6c892bb430c4645980e8890609950"
ORIGINAL_1_1_0_SHA256 = "6bd159eacb78bee2ff79c8216a6fc10ef68cc9adff16c7f53cec18d830f7c624"

# The suggested backward-resolution pins (spec Phase B config evolution).
DEFAULT_HISTORY_PINS = {
    "rewrite_window_user_turns": 3,
    "rewrite_char_budget": 1_500,
    "prompt_window_turns": 6,
    "prompt_char_budget": 6_000,
}


def test_released_configs_still_resolve_with_history_defaults_and_untouched_bytes():
    """1.0.0 AND 1.1.0 resolve (history pins fill from defaults); bytes immutable."""
    from app.config import load_config_manifest, resolve_pipeline_config

    manifest = load_config_manifest()
    for filename, original_hash in (
        ("legal-rag-default-1.0.0.yaml", ORIGINAL_1_0_0_SHA256),
        ("legal-rag-default-1.1.0.yaml", ORIGINAL_1_1_0_SHA256),
    ):
        raw = (CONFIGS_DIR / filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == original_hash, filename
        assert manifest[filename] == original_hash, filename
        # The released YAMLs carry NO history block: the pins fill from
        # schema defaults (backward RESOLUTION only, never for authoring).
        assert b"history" not in raw, filename

    for ref in ("legal-rag-default-1.0.0", "legal-rag-default-1.1.0"):
        history = resolve_pipeline_config(ref).config.history
        assert history.model_dump() == DEFAULT_HISTORY_PINS, ref


def test_1_2_0_resolves_with_every_pin_explicit_carrying_1_1_0_forward():
    """1.2.0 pins everything explicitly: 1.1.0's pins unchanged + history block."""
    from app.config import resolve_pipeline_config

    config_110 = resolve_pipeline_config("legal-rag-default-1.1.0").config
    resolved = resolve_pipeline_config("legal-rag-default-1.2.0")
    config = resolved.config

    assert resolved.pipeline_version == "1.2.0"
    # Generator/embedder/retrieval/context/guardrails pins carried forward
    # unchanged from 1.1.0.
    assert config.region == config_110.region
    assert config.generator == config_110.generator
    assert config.embedder == config_110.embedder
    assert config.retrieval == config_110.retrieval
    assert config.context == config_110.context
    assert config.guardrails == config_110.guardrails
    # The history block is EXPLICIT in the YAML (not default-resolved).
    raw = (CONFIGS_DIR / "legal-rag-default-1.2.0.yaml").read_text(encoding="utf-8")
    for pin, value in DEFAULT_HISTORY_PINS.items():
        assert f"{pin}: {value}" in raw, pin
        assert getattr(config.history, pin) == value, pin


def test_1_2_0_config_sha256_matches_its_manifest_entry():
    """config_sha256 truncates the manifest-verified hash of the raw 1.2.0 bytes."""
    from app.config import CONFIG_SHA256_LENGTH, load_config_manifest, resolve_pipeline_config

    raw = (CONFIGS_DIR / "legal-rag-default-1.2.0.yaml").read_bytes()
    full_hash = hashlib.sha256(raw).hexdigest()
    assert load_config_manifest()["legal-rag-default-1.2.0.yaml"] == full_hash

    resolved = resolve_pipeline_config("legal-rag-default-1.2.0")
    assert resolved.config_sha256 == full_hash[:CONFIG_SHA256_LENGTH]


def test_1_2_0_template_carries_the_history_placeholder_under_the_hash():
    """The inlined 1.2.0 template has {history} alongside {context}/{question}."""
    from app.config import resolve_pipeline_config

    config = resolve_pipeline_config("legal-rag-default-1.2.0").config
    template = config.prompt_template.text
    assert "{history}" in template
    assert "{context}" in template
    assert "{question}" in template
    assert config.prompt_template.version == "1.2.0"

    # The template text is IN the hashed raw bytes — one changed template byte
    # changes config_sha256 (manifest verification would then fail loudly).
    raw = (CONFIGS_DIR / "legal-rag-default-1.2.0.yaml").read_text(encoding="utf-8")
    assert "{history}" in raw


def test_eval_lane_default_config_ref_is_still_1_1_0():
    """1.1.0 stays the eval lane's default; 1.2.0 is the UI's ref, never a default here."""
    from app.config import DEFAULT_PIPELINE_CONFIG_REF

    assert DEFAULT_PIPELINE_CONFIG_REF == "legal-rag-default-1.1.0"
