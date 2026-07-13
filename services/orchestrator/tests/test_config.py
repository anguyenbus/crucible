"""Pinned pipeline-config tests: immutability, resolution, error types, purity.

Focused checks only (per task 3.1) — exhaustive per-field validation tests are
intentionally skipped.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = SERVICE_ROOT / "app" / "configs"


def test_released_configs_are_immutable_manifest_hashes_match():
    """Every config file's recomputed sha256 matches the committed manifest."""
    from app.config import load_config_manifest

    manifest = load_config_manifest()
    config_files = sorted(CONFIGS_DIR.glob("*.yaml"))
    assert config_files, "at least one released config artifact must ship"

    recomputed = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in config_files}
    assert recomputed == manifest, (
        "Released pipeline configs are IMMUTABLE: a file under app/configs/ no "
        "longer matches the committed manifest.sha256. Never edit a released "
        "config — any change requires a NEW {name}-{semver} file (plus a new "
        "manifest entry)."
    )


def test_resolve_returns_full_pinned_config_and_truncated_sha256():
    """resolve() returns every behavior pin plus the truncated content hash."""
    from app.config import (
        CONFIG_SHA256_LENGTH,
        load_config_manifest,
        resolve_pipeline_config,
    )

    resolved = resolve_pipeline_config("legal-rag-default-1.0.0")
    config = resolved.config

    assert config.generator.model_id == "au.anthropic.claude-sonnet-4-6"
    assert isinstance(config.generator.temperature, float)
    assert config.embedder.model_id == "amazon.titan-embed-text-v2:0"
    assert config.region == "ap-southeast-2"
    assert config.prompt_template.version and config.prompt_template.text
    assert config.retrieval.top_k > 0
    assert config.guardrails.policy_version
    assert resolved.pipeline_version == "1.0.0"

    # config_sha256 is the truncated sha256 of the RAW config file bytes —
    # i.e. a truncation of the committed manifest hash for that file.
    manifest = load_config_manifest()
    full_hash = manifest["legal-rag-default-1.0.0.yaml"]
    assert resolved.config_sha256 == full_hash[:CONFIG_SHA256_LENGTH]


def test_malformed_and_unknown_refs_raise_distinct_error_types():
    """Malformed ref → MalformedConfigRefError (422); unknown ref → UnknownConfigError (404)."""
    from app.config import (
        MalformedConfigRefError,
        UnknownConfigError,
        resolve_pipeline_config,
    )

    with pytest.raises(MalformedConfigRefError):
        resolve_pipeline_config("not_a_ref")
    with pytest.raises(UnknownConfigError):
        resolve_pipeline_config("no-such-config-9.9.9")

    # Distinct types: the router maps them to 422 vs 404 respectively.
    assert not issubclass(MalformedConfigRefError, UnknownConfigError)
    assert not issubclass(UnknownConfigError, MalformedConfigRefError)


def test_resolution_is_cwd_independent(monkeypatch, tmp_path):
    """Resolution goes through importlib.resources — no CWD assumptions."""
    monkeypatch.chdir(tmp_path)

    from app.config import resolve_pipeline_config

    resolved = resolve_pipeline_config("legal-rag-default-1.0.0")
    assert resolved.pipeline_version == "1.0.0"
    assert resolved.config.name == "legal-rag-default"


def test_import_app_config_requires_no_environment():
    """``import app.config`` (and get_settings) works with zero env vars set."""
    code = (
        "import app.config; "
        "settings = app.config.get_settings(); "
        "assert settings.opensearch_endpoint is None; "
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


# ---------------------------------------------------------------------------
# Manifest-verification (config integrity) and caching tests. Any test that
# monkeypatches resolver internals must clear the lru_caches before AND after
# so cached results never leak across tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def clear_config_caches():
    """Clear resolver caches around tests that monkeypatch config internals."""
    from app.config import load_config_manifest, resolve_pipeline_config

    resolve_pipeline_config.cache_clear()
    load_config_manifest.cache_clear()
    yield
    resolve_pipeline_config.cache_clear()
    load_config_manifest.cache_clear()


def test_tampered_config_bytes_raise_config_integrity_error(monkeypatch, clear_config_caches):
    """Bytes that hash differently from the manifest entry → ConfigIntegrityError."""
    import app.config
    from app.config import ConfigIntegrityError, resolve_pipeline_config

    real_bytes = (CONFIGS_DIR / "legal-rag-default-1.0.0.yaml").read_bytes()
    monkeypatch.setattr(
        app.config, "_read_packaged_config_bytes", lambda fname: real_bytes + b"# tampered\n"
    )

    with pytest.raises(ConfigIntegrityError, match="immutable"):
        resolve_pipeline_config("legal-rag-default-1.0.0")


def test_corrupt_yaml_bytes_raise_config_integrity_error(monkeypatch, clear_config_caches):
    """Manifest-matching but unparseable YAML → ConfigIntegrityError (not YAMLError)."""
    import app.config
    from app.config import ConfigIntegrityError, resolve_pipeline_config

    corrupt = b"key: [unclosed\n"
    corrupt_hash = hashlib.sha256(corrupt).hexdigest()
    monkeypatch.setattr(app.config, "_read_packaged_config_bytes", lambda fname: corrupt)
    # Make the manifest vouch for the corrupt bytes so the YAML branch is reached.
    monkeypatch.setattr(
        app.config,
        "load_config_manifest",
        lambda: {"legal-rag-default-1.0.0.yaml": corrupt_hash},
    )

    with pytest.raises(ConfigIntegrityError, match="Invalid YAML"):
        resolve_pipeline_config("legal-rag-default-1.0.0")


def test_config_missing_from_manifest_raises_config_integrity_error(
    monkeypatch, clear_config_caches
):
    """A packaged config absent from the manifest → ConfigIntegrityError."""
    import app.config
    from app.config import ConfigIntegrityError, resolve_pipeline_config

    monkeypatch.setattr(app.config, "load_config_manifest", dict)

    with pytest.raises(ConfigIntegrityError, match="must be listed in the manifest"):
        resolve_pipeline_config("legal-rag-default-1.0.0")


def test_resolution_is_cached_same_object_identity(clear_config_caches):
    """Two resolutions of the same ref return the SAME (frozen) cached object."""
    from app.config import resolve_pipeline_config

    first = resolve_pipeline_config("legal-rag-default-1.0.0")
    second = resolve_pipeline_config("legal-rag-default-1.0.0")
    assert first is second
