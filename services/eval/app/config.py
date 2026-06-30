"""
Configuration loading and environment variable expansion.

This module handles loading eval_config.yaml and expanding environment
variables in path values.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml

# Required top-level sections in eval_config.yaml
REQUIRED_SECTIONS = {"datasets", "metrics", "models"}


def expand_env_vars(value: str, default: str | None = None) -> str:
    """
    Expand environment variables in a string.

    Supports ${VAR_NAME} and ${VAR_NAME:-default} syntax.
    If VAR_NAME is not set and no default is provided, raises ValueError.

    Args:
        value: String containing ${VAR_NAME} or ${VAR_NAME:-default} references.
        default: Global default if env var is not set.

    Returns:
        String with all environment variables expanded.

    Raises:
        ValueError: If a referenced environment variable is not set and no default.

    Examples:
        >>> expand_env_vars("${HOME}/data")
        '/home/user/data'
        >>> expand_env_vars("/plain/path")
        '/plain/path'

    """
    # Pattern matches ${VAR_NAME} or ${VAR_NAME:-default}
    pattern = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

    def replace_var(match: re.Match[str]) -> str:
        var_name = match.group(1)
        yaml_default = match.group(2)  # Default from YAML, if present

        # Use YAML default first, then global default, then raise error
        if var_name in os.environ:
            return os.environ[var_name]
        elif yaml_default is not None:
            return yaml_default
        elif default is not None:
            return default
        else:
            raise ValueError(
                f"Environment variable '{var_name}' is referenced but not set. "
                f"Please set it or provide a default value."
            )

    return pattern.sub(replace_var, value)


def _expand_env_vars_recursive(data: Any, default: str | None = None) -> Any:
    """
    Recursively expand environment variables in a nested structure.

    Args:
        data: Any Python data structure (dict, list, str, etc.).
        default: Global default value for missing env vars (for testing).

    Returns:
        Same structure with env vars expanded in string values.

    """
    if isinstance(data, str):
        return expand_env_vars(data, default)
    elif isinstance(data, dict):
        return {k: _expand_env_vars_recursive(v, default) for k, v in data.items()}
    elif isinstance(data, list):
        return [_expand_env_vars_recursive(item, default) for item in data]
    else:
        return data


def load_config(
    config_path: Path,
    default_env_val: str | None = None,
    from_dotenv: bool = False,
) -> dict:
    """
    Load and parse eval_config.yaml.

    Expands environment variables in all string values. Validates that
    required top-level sections are present.

    Args:
        config_path: Path to eval_config.yaml file.
        default_env_val: Global default value for missing env vars (for testing).
        from_dotenv: When True, load a ``.env`` file via ``python-dotenv`` before
            expanding env vars. Library default is False -- library code must not
            load dotenv at import time; sanctioned entrypoints (``local/cli/*``)
            already load env early, so they call with the default. Opt-in
            callers that want config to honour a ``.env`` pass ``from_dotenv=True``.

    Returns:
        Parsed configuration dictionary with env vars expanded.

    Raises:
        FileNotFoundError: If config file does not exist.
        ValueError: If config is empty, invalid YAML, or missing required sections.

    """
    # Opt-in only: library default never loads dotenv at import or call time.
    # Sanctioned entrypoints load env earlier; this guard keeps the service
    # library pure unless a caller explicitly asks for .env expansion.
    if from_dotenv:
        from dotenv import load_dotenv

        _ = load_dotenv()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    content = config_path.read_text()
    if not content.strip():
        raise ValueError("Config file is empty or invalid")

    try:
        config = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in config file: {e}") from e

    if not isinstance(config, dict):
        raise ValueError("Config file must contain a YAML dictionary")

    # Check required sections
    missing_sections = REQUIRED_SECTIONS - set(config.keys())
    if missing_sections:
        missing = ", ".join(sorted(missing_sections))
        raise ValueError(f"Missing required sections in config: {missing}")

    # Expand environment variables recursively
    config = _expand_env_vars_recursive(config, default_env_val)

    return config


# ---------------------------------------------------------------------------
# Env-driven infra settings (Phase 1 restructure).
#
# These expose the infra endpoints from the environment so that LOCAL vs the
# MONOREPO deployment differ by ENV ONLY, not by code. The settings surface is
# intentionally lightweight (stdlib dataclass; no extra deps). Nothing in this
# package is REQUIRED to consume these yet -- runtime wiring (RDS-backed store,
# Phoenix write-back endpoint, ingestion calls) lands in Phase 2. The
# eval_config.yaml loader above and its ${VAR:-default} expansion semantics are
# UNCHANGED; this only ADDS the env-driven settings surface.
# ---------------------------------------------------------------------------
from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class Settings:
    """Infra endpoints resolved from the environment.

    Local and monorepo deployments differ ONLY by these env values, never by
    code. All fields default to None / a sentinel so importing this module never
    requires the environment to be configured (settings are exposed, not
    consumed, in this phase).
    """

    # RDS / run store (system of record for the run ledger in Phase 2).
    database_url: str | None = None
    # Phoenix observability write-back endpoint.
    phoenix_endpoint: str | None = None
    # Upstream ingestion / RAG service the worker calls to score runs.
    rag_endpoint: str | None = None
    # AWS region for the Bedrock judge (no us-east-1 default -- data residency).
    bedrock_region: str | None = None


def get_settings() -> Settings:
    """Build a :class:`Settings` snapshot from the current environment.

    Reads each endpoint from its environment variable; missing values stay
    None. AWS region resolves from AWS_REGION then AWS_DEFAULT_REGION (matching
    the Bedrock judge's native resolution).
    """
    return Settings(
        database_url=os.environ.get("DATABASE_URL"),
        phoenix_endpoint=os.environ.get("PHOENIX_ENDPOINT"),
        rag_endpoint=os.environ.get("RAG_ENDPOINT") or os.environ.get("INGESTION_ENDPOINT"),
        bedrock_region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
    )
