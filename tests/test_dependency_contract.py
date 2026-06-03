"""Tests for the dependency-pin contract (Bedrock-default-provider spec, Task Group 1).

These assert the deepeval pin is EXACT, that the pin matches the resolved uv.lock
version, and that the bedrock extra declares boto3. They parse pyproject.toml/uv.lock
via tomllib; they do not test pip/uv internals.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"


def _project_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["dependencies"]


def _deepeval_spec() -> str:
    for dep in _project_dependencies():
        if re.match(r"^\s*deepeval\b", dep):
            return dep.strip()
    pytest.fail("deepeval not found in [project.dependencies]")


def test_deepeval_is_pinned_exactly():
    """deepeval must use an exact `==` pin, not `>=`/`~=`/`<`."""
    spec = _deepeval_spec()
    assert "==" in spec, f"deepeval must be pinned with '==', got: {spec!r}"
    for loose in (">=", "~=", "<=", ">"):
        assert loose not in spec, f"deepeval must not use {loose!r}: {spec!r}"
    # No range markers (commas separate range constraints).
    assert "," not in spec, f"deepeval must be a single exact pin: {spec!r}"


def test_deepeval_pin_matches_uv_lock():
    """The exact pin in pyproject.toml must match the resolved version in uv.lock."""
    spec = _deepeval_spec()
    pinned = spec.split("==", 1)[1].strip().strip('"')

    lock = tomllib.loads(UV_LOCK.read_text())
    locked_versions = {
        pkg["name"]: pkg["version"] for pkg in lock["package"] if pkg["name"] == "deepeval"
    }
    assert "deepeval" in locked_versions, "deepeval missing from uv.lock"
    assert locked_versions["deepeval"] == pinned, (
        f"pyproject pin {pinned!r} does not match uv.lock {locked_versions['deepeval']!r}"
    )


def test_bedrock_extra_declares_boto3():
    """The `bedrock` optional-dependency extra must declare boto3."""
    data = tomllib.loads(PYPROJECT.read_text())
    extras = data["project"]["optional-dependencies"]
    assert "bedrock" in extras, "missing `bedrock` optional-dependency extra"
    assert any(re.match(r"^\s*boto3\b", dep) for dep in extras["bedrock"]), (
        f"bedrock extra must declare boto3, got: {extras['bedrock']!r}"
    )
