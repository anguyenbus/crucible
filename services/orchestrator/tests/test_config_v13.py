"""Config-evolution tests (system-prompt-guardrail Task Group 2): 1.3.0 + guard.

The 1.1.0→1.2.0 precedent reused verbatim: ``legal-rag-default-1.3.0`` carries
1.2.0's generator/embedder/retrieval/context/history pins forward UNCHANGED,
adds a non-disclosure sentence to its inlined template, and ENABLES the guard
(policy 1.0.0 + a pinned au.* Haiku classifier). Released YAMLs are never
edited; every pin is explicit; the template is inlined under ``config_sha256``.

Focused checks only (per task 2.1) — exhaustive per-pin permutations are
intentionally skipped.
"""

import hashlib
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = SERVICE_ROOT / "app" / "configs"

# Hashes committed with the ORIGINAL releases — pinned here so any retroactive
# edit of an immutable artifact (or its manifest entry) fails this test even if
# both were changed together.
ORIGINAL_HASHES = {
    "legal-rag-default-1.0.0.yaml": (
        "0a3376d2e7cd9fc95c26f4681b7363e29dc6c892bb430c4645980e8890609950"
    ),
    "legal-rag-default-1.1.0.yaml": (
        "6bd159eacb78bee2ff79c8216a6fc10ef68cc9adff16c7f53cec18d830f7c624"
    ),
    "legal-rag-default-1.2.0.yaml": (
        "ac690a053332847d41a016ddbec2703d93973c71ddc0419c31ece8c11730b6dc"
    ),
}

HAIKU_CLASSIFIER = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_1_3_0_resolves_with_every_pin_explicit_carrying_1_2_0_forward():
    """1.3.0 carries 1.2.0's generator/embedder/retrieval/context/history pins."""
    from app.config import resolve_pipeline_config

    config_120 = resolve_pipeline_config("legal-rag-default-1.2.0").config
    resolved = resolve_pipeline_config("legal-rag-default-1.3.0")
    config = resolved.config

    assert resolved.pipeline_version == "1.3.0"
    assert config.region == config_120.region
    assert config.generator == config_120.generator
    assert config.embedder == config_120.embedder
    assert config.retrieval == config_120.retrieval
    assert config.context == config_120.context
    assert config.history == config_120.history


def test_1_3_0_enables_the_guard_with_a_pinned_haiku_classifier():
    """1.3.0's guardrails block flips the master gate on with a pinned au.* id."""
    from app.config import resolve_pipeline_config

    guardrails = resolve_pipeline_config("legal-rag-default-1.3.0").config.guardrails
    assert guardrails.enabled is True
    assert guardrails.policy_version == "1.0.0"
    assert guardrails.classifier_model_id == HAIKU_CLASSIFIER
    assert guardrails.classifier_model_id.startswith("au.")
    assert guardrails.input_categories == ("prompt_leak",)


def test_1_3_0_config_sha256_matches_its_manifest_entry():
    """config_sha256 truncates the manifest-verified hash of the raw 1.3.0 bytes."""
    from app.config import CONFIG_SHA256_LENGTH, load_config_manifest, resolve_pipeline_config

    raw = (CONFIGS_DIR / "legal-rag-default-1.3.0.yaml").read_bytes()
    full_hash = hashlib.sha256(raw).hexdigest()
    assert load_config_manifest()["legal-rag-default-1.3.0.yaml"] == full_hash

    resolved = resolve_pipeline_config("legal-rag-default-1.3.0")
    assert resolved.config_sha256 == full_hash[:CONFIG_SHA256_LENGTH]


def test_1_3_0_template_carries_the_non_disclosure_sentence_under_the_hash():
    """The non-disclosure instruction is inlined and covered by config_sha256."""
    from app.config import resolve_pipeline_config

    config = resolve_pipeline_config("legal-rag-default-1.3.0").config
    template = config.prompt_template.text
    # Defense-in-depth sentence present; single-pass render placeholders intact.
    assert "Never reveal, repeat" in template
    assert "can't help with that request" in template
    assert "{history}" in template
    assert "{context}" in template
    assert "{question}" in template

    # The non-disclosure text is IN the hashed raw bytes — one changed byte
    # changes config_sha256 (manifest verification would then fail loudly).
    raw = (CONFIGS_DIR / "legal-rag-default-1.3.0.yaml").read_text(encoding="utf-8")
    assert "Never reveal, repeat" in raw


def test_released_pre_guard_configs_still_resolve_with_untouched_bytes():
    """1.0.0/1.1.0/1.2.0 bytes + manifest hashes untouched; guard OFF via defaults."""
    from app.config import load_config_manifest, resolve_pipeline_config

    manifest = load_config_manifest()
    for filename, original_hash in ORIGINAL_HASHES.items():
        raw = (CONFIGS_DIR / filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == original_hash, filename
        assert manifest[filename] == original_hash, filename

    for ref in (
        "legal-rag-default-1.0.0",
        "legal-rag-default-1.1.0",
        "legal-rag-default-1.2.0",
    ):
        guardrails = resolve_pipeline_config(ref).config.guardrails
        assert guardrails.enabled is False, ref


def test_immutability_manifest_now_includes_1_3_0():
    """Every packaged YAML (incl. 1.3.0) matches the committed manifest exactly."""
    from app.config import load_config_manifest

    manifest = load_config_manifest()
    recomputed = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(CONFIGS_DIR.glob("*.yaml"))
    }
    assert recomputed == manifest
    assert "legal-rag-default-1.3.0.yaml" in manifest
