"""
Service configuration: env-driven Settings and pinned pipeline-config resolution.

DIVISION OF LABOR (never blurred):
- ``Settings``/env carry LOCATION FACTS only (BYO index contract: where
  OpenSearch lives, what its index/pipeline/fields are named, the Phoenix
  endpoint). Local and monorepo deployments differ ONLY by these env values.
- Pinned YAML configs (``app/configs/{name}-{semver}.yaml``) carry BEHAVIOR
  only (generator/embedder model pins, inline prompt template, top_k,
  retrieval params, context character budget, max_tokens, guardrail policy
  version, temperature). Identical config ⇒ identical behavior; a behavior
  change requires publishing a NEW config version.

Config resolution is by exact fully-qualified ``{name}-{semver}`` reference
via ``importlib.resources`` against the packaged ``app.configs`` — no floating
refs, no ``latest`` alias, no default-to-newest, no fallback, no CWD-relative
paths. Every resolution verifies the artifact's FULL sha256 against the
committed hash manifest (released configs are immutable) and returns a
``config_sha256`` (truncation of that same verified hash) that is echoed in
``result.system_version`` so historical runs carry the hash of the config that
actually ran.

Importing this module performs NO environment reads and NO I/O (eval's
discipline); ``load_dotenv`` runs only behind an explicit ``from_dotenv=True``
opt-in. Resolution results are ``lru_cache``d — the returned objects are
frozen, and ``lru_cache`` never caches exceptions, so failures are re-checked
on every call.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
from dataclasses import dataclass
from importlib import resources
from typing import Final

import yaml
from pydantic import ValidationError

from app.schemas.pipeline_config import PipelineConfig
from app.schemas.query import PIPELINE_CONFIG_REF_RE

# Packaged config artifacts live in the in-package directory that ships in the
# wheel (source-include'd in pyproject.toml).
_CONFIGS_PACKAGE: Final[str] = "app.configs"

# Committed hash manifest: `sha256sum` format lines mapping each released
# config filename to its FULL sha256. Resolution verifies every read against
# it (and the immutability test recomputes hashes against it); the truncation
# of the verified hash is echoed in the response.
_MANIFEST_RESOURCE: Final[str] = "manifest.sha256"
_MANIFEST_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<sha256>[0-9a-f]{64})[ *]+(?P<filename>\S+)$"
)

# Length of the truncated config_sha256 echoed in result.system_version.
CONFIG_SHA256_LENGTH: Final[int] = 12

# The documented default config: Phase 2 acceptance runs against it, eval's
# ORCHESTRATOR_PIPELINE_CONFIG defaults to it, and lifespan uses its pins for
# construction-time client facts (region, query-side embedder for the _meta
# guard). Per-request behavior still comes from each request's resolved ref.
# The Chainlit demo UI defaults to "legal-rag-default-1.2.0" (the multi-turn
# history config) via its OWN env surface — the eval lane's default here
# stays 1.1.0, unchanged.
DEFAULT_PIPELINE_CONFIG_REF: Final[str] = "legal-rag-default-1.1.0"


class MalformedConfigRefError(ValueError):
    """
    The config reference does not match the ``{name}-{semver}`` shape.

    Mapped to HTTP 422 by the app-level exception handler.
    """


class UnknownConfigError(LookupError):
    """
    No released config exists for a well-formed ``{name}-{semver}`` reference.

    Exact match only — never resolved by fallback or a ``latest`` alias.
    Mapped to HTTP 404 by the app-level exception handler.
    """


class ConfigIntegrityError(Exception):
    """
    A packaged config artifact is corrupt or does not match the manifest.

    This is a SERVICE defect (bad build/packaging or a tampered released
    artifact), never a caller error. Mapped to HTTP 500 by the app-level
    exception handler.
    """


@dataclass(frozen=True)
class ResolvedPipelineConfig:
    """A resolved, pinned pipeline config plus its provenance anchors."""

    # Full behavior pins parsed from the YAML artifact.
    config: PipelineConfig
    # The config's semver; feeds result.system_version.pipeline_version.
    pipeline_version: str
    # Truncated sha256 of the RAW config file bytes (manifest-verified); feeds
    # result.system_version.config_sha256 so retroactive config edits visibly
    # mismatch recorded runs.
    config_sha256: str


def _read_packaged_config_bytes(filename: str) -> bytes:
    """
    Read a packaged config artifact's raw bytes via ``importlib.resources``.

    Kept as a seam so tests can monkeypatch the byte source (e.g. to simulate
    a tampered or corrupt artifact) without touching packaged files.

    Raises:
        FileNotFoundError: If no such artifact is packaged.

    """
    resource = resources.files(_CONFIGS_PACKAGE).joinpath(filename)
    if not resource.is_file():
        raise FileNotFoundError(f"Packaged config not found: {filename} in {_CONFIGS_PACKAGE}")
    return resource.read_bytes()


@functools.lru_cache
def resolve_pipeline_config(ref: str) -> ResolvedPipelineConfig:
    """
    Resolve a fully-qualified ``{name}-{semver}`` config reference.

    Performs an exact-match lookup of ``<ref>.yaml`` in the packaged
    ``app.configs`` via ``importlib.resources`` (no CWD assumptions), then
    verifies the artifact's FULL sha256 against the committed manifest before
    parsing. Results are cached (frozen objects; exceptions are never cached).

    Args:
        ref: Fully-qualified reference, e.g. ``"legal-rag-default-1.0.0"``.

    Returns:
        The parsed pins, the config's semver, and the truncated content hash.

    Raises:
        MalformedConfigRefError: If ``ref`` is not ``{name}-{semver}`` shaped.
        UnknownConfigError: If no released config file matches ``ref`` exactly.
        ConfigIntegrityError: If the artifact is missing from the manifest,
            does not match its committed hash, is not valid YAML, or does not
            parse as a :class:`PipelineConfig` (incl. a filename that
            disagrees with the declared name/version) — a service defect,
            not a caller error.

    """
    match = PIPELINE_CONFIG_REF_RE.fullmatch(ref)
    if match is None:
        raise MalformedConfigRefError(
            f"Config reference {ref!r} is not a fully-qualified {{name}}-{{semver}} "
            f"reference (expected e.g. 'legal-rag-default-1.0.0')."
        )

    fname = f"{ref}.yaml"
    try:
        raw = _read_packaged_config_bytes(fname)
    except FileNotFoundError:
        raise UnknownConfigError(
            f"Unknown pipeline config {ref!r}: no released config file "
            f"'{fname}' exists (exact {{name}}-{{semver}} match only; "
            f"no fallback, no 'latest')."
        ) from None

    # Manifest verification: the FULL hash of the raw bytes must match the
    # committed manifest entry before the artifact is trusted at all.
    full_hash = hashlib.sha256(raw).hexdigest()
    manifest = load_config_manifest()
    if fname not in manifest:
        raise ConfigIntegrityError(
            f"Packaged config '{fname}' is not listed in {_MANIFEST_RESOURCE} — "
            f"released configs must be listed in the manifest."
        )
    if full_hash != manifest[fname]:
        raise ConfigIntegrityError(
            f"Packaged config '{fname}' does not match the committed manifest "
            f"— released configs are immutable; any change requires a NEW "
            f"{{name}}-{{semver}} file (plus a new manifest entry)."
        )

    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigIntegrityError(f"Invalid YAML in packaged config '{fname}': {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigIntegrityError(
            f"Packaged config '{fname}' must parse to a YAML mapping, got {type(loaded).__name__}."
        )

    try:
        config = PipelineConfig.model_validate(loaded)
    except ValidationError as exc:
        raise ConfigIntegrityError(f"Packaged config '{fname}' failed validation: {exc}") from exc

    if f"{config.name}-{config.version}" != ref:
        raise ConfigIntegrityError(
            f"Config artifact '{fname}' declares name/version "
            f"'{config.name}-{config.version}', which does not match its filename."
        )

    return ResolvedPipelineConfig(
        config=config,
        # PIPELINE_CONFIG_REF_RE groups: 1 = name, 2 = semver (plain groups —
        # the pattern is exported into OpenAPI, where named groups are invalid).
        pipeline_version=match.group(2),
        config_sha256=full_hash[:CONFIG_SHA256_LENGTH],
    )


@functools.lru_cache
def load_config_manifest() -> dict[str, str]:
    """
    Load the committed hash manifest from the packaged ``app.configs``.

    Cached after the first successful load (``lru_cache`` never caches
    exceptions, so a missing/malformed manifest is re-checked every call).

    Returns:
        Mapping of released config filename to its FULL sha256 hex digest.

    Raises:
        FileNotFoundError: If the manifest is not packaged.
        ValueError: If any manifest line is not ``sha256sum`` formatted.

    """
    resource = resources.files(_CONFIGS_PACKAGE).joinpath(_MANIFEST_RESOURCE)
    if not resource.is_file():
        raise FileNotFoundError(
            f"Config hash manifest not found: {_MANIFEST_RESOURCE} in {_CONFIGS_PACKAGE}"
        )

    manifest: dict[str, str] = {}
    for line in resource.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = _MANIFEST_LINE_RE.fullmatch(line.strip())
        if match is None:
            raise ValueError(
                f"Malformed manifest line in {_MANIFEST_RESOURCE}: {line!r} "
                f"(expected sha256sum format: '<sha256>  <filename>')"
            )
        manifest[match.group("filename")] = match.group("sha256")
    return manifest


# ---------------------------------------------------------------------------
# Env-driven infra settings (imitates services/eval/app/config.py).
#
# Settings carry LOCATION FACTS only (BYO index contract v2): where OpenSearch
# lives and what its index/pipeline/fields are NAMED, plus the shared Phoenix
# endpoint. Behavior (top_k, budgets, model ids, prompt template) lives ONLY
# in the pinned {name}-{semver} configs. Settings are resolved at FastAPI
# lifespan — never at import; precedence for any value is env > config file >
# default, and every field has a default so importing this module never
# requires a configured environment.
#
# ORCHESTRATOR_OPENSEARCH_* is the service-owned prefix; PHOENIX_ENDPOINT
# stays unprefixed (shared infra name matching eval and
# docker-compose.observability.yml).
# ---------------------------------------------------------------------------

# POC defaults for the BYO index location facts (docs/byo-index-contract.md).
_DEFAULT_OPENSEARCH_INDEX: Final[str] = "legal-rag-bench"
_DEFAULT_OPENSEARCH_PIPELINE: Final[str] = "hybrid-search-pipeline"
_DEFAULT_OPENSEARCH_TEXT_FIELD: Final[str] = "content"
_DEFAULT_OPENSEARCH_VECTOR_FIELD: Final[str] = "content_vector"


@dataclass(frozen=True)
class Settings:
    """
    Infra location facts resolved from the environment.

    Local and monorepo deployments differ ONLY by these env values, never by
    code. Location facts ONLY — behavior pins (models, prompts, top_k,
    budgets) live in the pinned YAML configs and the two never blur.
    """

    # OpenSearch domain endpoint for retrieval (BYO index contract).
    opensearch_endpoint: str | None = None
    # BYO index location facts: names of things, never behavior.
    opensearch_index: str = _DEFAULT_OPENSEARCH_INDEX
    opensearch_pipeline: str = _DEFAULT_OPENSEARCH_PIPELINE
    opensearch_text_field: str = _DEFAULT_OPENSEARCH_TEXT_FIELD
    opensearch_vector_field: str = _DEFAULT_OPENSEARCH_VECTOR_FIELD
    # Phoenix observability endpoint for span export (shared infra name).
    phoenix_endpoint: str | None = None
    # Out-of-process NeMo Guardrails pod base URL (a LOCATION fact, exactly
    # like opensearch_endpoint). None ⇒ the NeMo output/facts lane cannot be
    # reached; the lane is also config-gated (guardrails.nemo), so the
    # released 1.0.0-1.4.0 configs never call the pod regardless.
    nemo_guard_url: str | None = None


def get_settings(from_dotenv: bool = False) -> Settings:
    """
    Build a :class:`Settings` snapshot from the current environment.

    Reads each location fact from its environment variable; a missing
    endpoint stays ``None`` and the BYO index facts fall back to the POC
    defaults (precedence env > config file > default; there is no
    config-file source for these values).

    Args:
        from_dotenv: When True, load a ``.env`` file via ``python-dotenv``
            first. Library default is False — library code must never load
            dotenv at import time; only explicit opt-in callers get ``.env``
            expansion.

    """
    if from_dotenv:
        from dotenv import load_dotenv

        _ = load_dotenv()

    return Settings(
        opensearch_endpoint=os.environ.get("ORCHESTRATOR_OPENSEARCH_ENDPOINT"),
        opensearch_index=os.environ.get("ORCHESTRATOR_OPENSEARCH_INDEX", _DEFAULT_OPENSEARCH_INDEX),
        opensearch_pipeline=os.environ.get(
            "ORCHESTRATOR_OPENSEARCH_PIPELINE", _DEFAULT_OPENSEARCH_PIPELINE
        ),
        opensearch_text_field=os.environ.get(
            "ORCHESTRATOR_OPENSEARCH_TEXT_FIELD", _DEFAULT_OPENSEARCH_TEXT_FIELD
        ),
        opensearch_vector_field=os.environ.get(
            "ORCHESTRATOR_OPENSEARCH_VECTOR_FIELD", _DEFAULT_OPENSEARCH_VECTOR_FIELD
        ),
        phoenix_endpoint=os.environ.get("PHOENIX_ENDPOINT"),
        nemo_guard_url=os.environ.get("ORCHESTRATOR_NEMO_GUARD_URL"),
    )
