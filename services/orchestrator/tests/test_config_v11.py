"""Config-evolution tests (Phase 2): inline prompt template, 1.1.0, env surface.

Focused checks only (per task 1.1) — exhaustive per-field permutations are
intentionally skipped.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = SERVICE_ROOT / "app" / "configs"

# The hash committed with the ORIGINAL Phase 1 release of 1.0.0 — pinned here
# so any retroactive edit of the immutable artifact (or its manifest entry)
# fails this test even if both were changed together.
ORIGINAL_1_0_0_SHA256 = "0a3376d2e7cd9fc95c26f4681b7363e29dc6c892bb430c4645980e8890609950"


def test_1_0_0_still_resolves_with_schema_defaults_and_untouched_bytes():
    """1.0.0 resolves via schema defaults; its bytes and manifest hash are untouched."""
    from app.config import load_config_manifest, resolve_pipeline_config

    raw = (CONFIGS_DIR / "legal-rag-default-1.0.0.yaml").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ORIGINAL_1_0_0_SHA256
    assert load_config_manifest()["legal-rag-default-1.0.0.yaml"] == ORIGINAL_1_0_0_SHA256

    resolved = resolve_pipeline_config("legal-rag-default-1.0.0")
    config = resolved.config
    # New Phase 2 fields fill from schema defaults (backward RESOLUTION only).
    assert config.generator.max_tokens == 1024
    assert config.context.char_budget > 0
    assert "{context}" in config.prompt_template.text
    assert "{question}" in config.prompt_template.text


def test_1_1_0_resolves_every_pin_explicit_and_hash_covers_template():
    """1.1.0 carries every pin explicitly; config_sha256 covers the template bytes."""
    from app.config import (
        CONFIG_SHA256_LENGTH,
        load_config_manifest,
        resolve_pipeline_config,
    )

    resolved = resolve_pipeline_config("legal-rag-default-1.1.0")
    config = resolved.config

    assert config.generator.model_id == "au.anthropic.claude-sonnet-4-6"
    assert config.generator.temperature == 0.0
    assert config.generator.max_tokens == 1024
    assert config.embedder.model_id == "amazon.titan-embed-text-v2:0"
    assert config.region == "ap-southeast-2"
    assert config.retrieval.top_k == 8
    assert config.context.char_budget == 32000
    assert config.prompt_template.version == "1.1.0"
    # The template instructs bracketed verbatim chunk-id citation markers.
    assert "[doc_id:chunk_idx]" in config.prompt_template.text
    assert "{context}" in config.prompt_template.text
    assert "{question}" in config.prompt_template.text

    # config_sha256 truncates the manifest hash of the raw file bytes, and the
    # template text is IN those bytes — one changed template byte changes both.
    raw = (CONFIGS_DIR / "legal-rag-default-1.1.0.yaml").read_bytes()
    full_hash = hashlib.sha256(raw).hexdigest()
    assert load_config_manifest()["legal-rag-default-1.1.0.yaml"] == full_hash
    assert resolved.config_sha256 == full_hash[:CONFIG_SHA256_LENGTH]
    assert "You are a helpful assistant" in raw.decode("utf-8")


def test_prompt_template_pin_carries_version_and_text_without_ref():
    """PromptTemplatePin has version + text fields; the ref indirection is gone."""
    from app.schemas.pipeline_config import PromptTemplatePin

    fields = PromptTemplatePin.model_fields
    assert "version" in fields
    assert "text" in fields
    assert "ref" not in fields


def test_settings_expose_prefixed_opensearch_surface_with_poc_defaults(monkeypatch):
    """ORCHESTRATOR_OPENSEARCH_* is the env surface; bare OPENSEARCH_ENDPOINT is dead."""
    from app.config import get_settings

    monkeypatch.setenv("ORCHESTRATOR_OPENSEARCH_ENDPOINT", "https://os.example.com")
    monkeypatch.setenv("PHOENIX_ENDPOINT", "http://phoenix:6006")
    settings = get_settings()
    assert settings.opensearch_endpoint == "https://os.example.com"
    # POC defaults per docs/byo-index-contract.md when the vars are unset.
    assert settings.opensearch_index == "legal-rag-bench"
    assert settings.opensearch_pipeline == "hybrid-search-pipeline"
    assert settings.opensearch_text_field == "content"
    assert settings.opensearch_vector_field == "content_vector"
    # PHOENIX_ENDPOINT stays unprefixed (shared infra name).
    assert settings.phoenix_endpoint == "http://phoenix:6006"

    # The renamed bare var is never read: setting it must have no effect.
    monkeypatch.delenv("ORCHESTRATOR_OPENSEARCH_ENDPOINT")
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://must-be-ignored.example.com")
    assert get_settings().opensearch_endpoint is None

    # Overrides for the remaining location facts are honored.
    monkeypatch.setenv("ORCHESTRATOR_OPENSEARCH_INDEX", "other-index")
    monkeypatch.setenv("ORCHESTRATOR_OPENSEARCH_PIPELINE", "other-pipeline")
    monkeypatch.setenv("ORCHESTRATOR_OPENSEARCH_TEXT_FIELD", "body")
    monkeypatch.setenv("ORCHESTRATOR_OPENSEARCH_VECTOR_FIELD", "body_vector")
    settings = get_settings()
    assert settings.opensearch_index == "other-index"
    assert settings.opensearch_pipeline == "other-pipeline"
    assert settings.opensearch_text_field == "body"
    assert settings.opensearch_vector_field == "body_vector"


def test_import_app_config_with_zero_env_reads_nothing():
    """``import app.config`` + get_settings need no env; new fields default cleanly."""
    code = (
        "import app.config; "
        "settings = app.config.get_settings(); "
        "assert settings.opensearch_endpoint is None; "
        "assert settings.opensearch_index == 'legal-rag-bench'; "
        "assert settings.opensearch_pipeline == 'hybrid-search-pipeline'; "
        "assert settings.opensearch_text_field == 'content'; "
        "assert settings.opensearch_vector_field == 'content_vector'; "
        "assert settings.phoenix_endpoint is None"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SERVICE_ROOT,
        env={"PYTHONPATH": str(SERVICE_ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"`import app.config` must not require a configured environment:\n{result.stderr}"
    )
