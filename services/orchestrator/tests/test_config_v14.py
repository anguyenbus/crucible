"""Config-evolution tests (output+input-hardening Task Group 5): 1.4.0.

The 1.2.0→1.3.0 precedent reused verbatim: ``legal-rag-default-1.4.0`` carries
1.3.0's generator/embedder/retrieval/context/history AND prompt_template pins
forward UNCHANGED (this slice does NOT touch prompt hardening, so the inlined
template is BYTE-IDENTICAL to 1.3.0's), and changes ONLY the guardrails block —
bumping policy_version to 2.0.0, opting the hardened pre-filter in via
``input_categories=[prompt_leak, jailbreak, unicode_evasion]`` and the
deterministic output scan in via ``output_categories=[pii, secrets]``, while
keeping the SAME enabled au.* Haiku classifier. Released YAMLs are never
edited; every pin is explicit; the template is inlined under ``config_sha256``.

Focused checks only (per task 5.1) — exhaustive per-pin permutations are
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
    "legal-rag-default-1.3.0.yaml": (
        "0249750b5064823da6f7fbf57924681b8cdeb3b6ff67e28a05cf529f90bb0cfd"
    ),
}

HAIKU_CLASSIFIER = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_1_4_0_resolves_with_every_pin_explicit_carrying_1_3_0_forward():
    """1.4.0 carries 1.3.0's generator/embedder/retrieval/context/history pins."""
    from app.config import resolve_pipeline_config

    config_130 = resolve_pipeline_config("legal-rag-default-1.3.0").config
    resolved = resolve_pipeline_config("legal-rag-default-1.4.0")
    config = resolved.config

    assert resolved.pipeline_version == "1.4.0"
    assert config.region == config_130.region
    assert config.generator == config_130.generator
    assert config.embedder == config_130.embedder
    assert config.retrieval == config_130.retrieval
    assert config.context == config_130.context
    assert config.history == config_130.history


def test_1_4_0_config_sha256_matches_its_manifest_entry():
    """config_sha256 truncates the manifest-verified hash of the raw 1.4.0 bytes."""
    from app.config import CONFIG_SHA256_LENGTH, load_config_manifest, resolve_pipeline_config

    raw = (CONFIGS_DIR / "legal-rag-default-1.4.0.yaml").read_bytes()
    full_hash = hashlib.sha256(raw).hexdigest()
    assert load_config_manifest()["legal-rag-default-1.4.0.yaml"] == full_hash

    resolved = resolve_pipeline_config("legal-rag-default-1.4.0")
    assert resolved.config_sha256 == full_hash[:CONFIG_SHA256_LENGTH]


def test_1_4_0_guardrails_block_opts_into_output_and_hardened_input():
    """1.4.0's guardrails block: policy 2.0.0, same Haiku id, both detector sets."""
    from app.config import resolve_pipeline_config

    guardrails = resolve_pipeline_config("legal-rag-default-1.4.0").config.guardrails
    assert guardrails.enabled is True
    assert guardrails.policy_version == "2.0.0"
    assert guardrails.classifier_model_id == HAIKU_CLASSIFIER
    assert guardrails.classifier_model_id.startswith("au.")
    assert guardrails.input_categories == ("prompt_leak", "jailbreak", "unicode_evasion")
    assert guardrails.output_categories == ("pii", "secrets")


def test_1_4_0_template_is_byte_identical_to_1_3_0():
    """This slice does NOT touch prompt hardening: the template bytes are unchanged."""
    from app.config import resolve_pipeline_config

    template_130 = resolve_pipeline_config("legal-rag-default-1.3.0").config.prompt_template
    template_140 = resolve_pipeline_config("legal-rag-default-1.4.0").config.prompt_template
    # Byte-identical text — one changed template byte would change config_sha256.
    assert template_140.text == template_130.text
    assert template_140.version == template_130.version


def test_1_0_0_through_1_3_0_still_resolve_with_untouched_bytes_and_guards_off():
    """1.0.0-1.3.0 bytes + manifest hashes untouched; output guard OFF via defaults."""
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
        "legal-rag-default-1.3.0",
    ):
        guardrails = resolve_pipeline_config(ref).config.guardrails
        # The output guard stays OFF for every pre-1.4.0 config.
        assert guardrails.output_categories == (), ref


def test_immutability_manifest_now_includes_1_4_0():
    """Every packaged YAML (incl. 1.4.0) matches the committed manifest exactly."""
    from app.config import load_config_manifest

    manifest = load_config_manifest()
    recomputed = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(CONFIGS_DIR.glob("*.yaml"))
    }
    assert recomputed == manifest
    assert "legal-rag-default-1.4.0.yaml" in manifest
