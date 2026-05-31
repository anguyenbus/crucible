"""Tests for configuration module."""

import os
from pathlib import Path
import tempfile

import pytest
import yaml

from crucible.config import (
    REQUIRED_SECTIONS,
    expand_env_vars,
    load_config,
)


def test_expand_env_vars_with_set_variable():
    """Test environment variable expansion when variable is set."""
    os.environ["TEST_VAR"] = "test_value"
    result = expand_env_vars("${TEST_VAR}/data")
    assert result == "test_value/data"
    del os.environ["TEST_VAR"]


def test_expand_env_vars_with_default():
    """Test environment variable expansion with default value."""
    result = expand_env_vars("${UNSET_VAR:-default_value}")
    assert result == "default_value"


def test_expand_env_vars_raises_on_missing():
    """Test that expansion raises when variable is missing without default."""
    with pytest.raises(ValueError, match="UNSET_VAR.*not set"):
        expand_env_vars("${UNSET_VAR}/path")


def test_expand_env_vars_plain_string():
    """Test that plain strings pass through unchanged."""
    result = expand_env_vars("/plain/path")
    assert result == "/plain/path"


def test_load_config_with_valid_yaml(tmp_path: Path):
    """Test loading a valid config file."""
    config_content = """
datasets:
  legal_rAG_bench:
    path: /data/legal

metrics:
  - faithfulness
  - contextual_precision

models:
  openai:
    model: gpt-4o
"""
    config_file = tmp_path / "eval_config.yaml"
    config_file.write_text(config_content)

    result = load_config(config_file)

    assert result["datasets"]["legal_rAG_bench"]["path"] == "/data/legal"
    assert "faithfulness" in str(result["metrics"])


def test_load_config_with_env_vars(tmp_path: Path):
    """Test loading config with environment variable expansion."""
    os.environ["DATA_DIR"] = "/opt/data"

    config_content = """
datasets:
  legal_rAG_bench:
    path: ${DATA_DIR}/legal

metrics:
  - faithfulness

models:
  openai:
    model: gpt-4o
"""
    config_file = tmp_path / "eval_config.yaml"
    config_file.write_text(config_content)

    result = load_config(config_file)

    assert result["datasets"]["legal_rAG_bench"]["path"] == "/opt/data/legal"
    del os.environ["DATA_DIR"]


def test_load_config_missing_file(tmp_path: Path):
    """Test that loading missing file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nonexistent.yaml")


def test_load_config_missing_required_sections(tmp_path: Path):
    """Test that missing required sections raises ValueError."""
    config_content = """
datasets:
  legal_rAG_bench:
    path: /data/legal
"""
    config_file = tmp_path / "eval_config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="Missing required sections"):
        load_config(config_file)


def test_required_sections_constant():
    """Test that REQUIRED_SECTIONS contains expected values."""
    assert REQUIRED_SECTIONS == {"datasets", "metrics", "models"}
