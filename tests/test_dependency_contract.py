"""Tests for the dependency-pin and dependency-shape contract.

These assert the deepeval pin is EXACT, that the pin matches the resolved uv.lock
version, and that the bedrock extra declares boto3 (Bedrock-default-provider spec,
Task Group 1). Phase 5 extends them with the kernel-grade core shape: demo-only
deps must NOT be in core, a `demo` extra must carry them, and `observability` must
have been renamed to `phoenix`. They parse pyproject.toml/uv.lock via tomllib; they
do not test pip/uv internals.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

# Demo-only deps quarantined out of core in Phase 5. These must NEVER reappear in
# [project.dependencies]; `pip install crucible` (no extras) stays kernel-grade.
DEMO_ONLY_DEPS = ("chromadb", "sentence-transformers", "datasets")


def _project_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["dependencies"]


def _optional_dependencies() -> dict[str, list[str]]:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["optional-dependencies"]


def _dep_names(specs: list[str]) -> set[str]:
    """Extract bare package names from a list of PEP 508 requirement strings."""
    names: set[str] = set()
    for spec in specs:
        match = re.match(r"^\s*([A-Za-z0-9._-]+)", spec)
        if match:
            names.add(match.group(1).lower())
    return names


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
    extras = _optional_dependencies()
    assert "bedrock" in extras, "missing `bedrock` optional-dependency extra"
    assert any(re.match(r"^\s*boto3\b", dep) for dep in extras["bedrock"]), (
        f"bedrock extra must declare boto3, got: {extras['bedrock']!r}"
    )


def test_demo_only_deps_not_in_core():
    """Demo-only deps must NOT leak into core [project.dependencies] (Phase 5).

    `pip install crucible` (no extras) must install a kernel-grade core; chromadb,
    sentence-transformers, and datasets are demo-only and live in `demo`.
    """
    core = _dep_names(_project_dependencies())
    for dep in DEMO_ONLY_DEPS:
        assert dep not in core, (
            f"{dep!r} must NOT be in core [project.dependencies]; it belongs in the `demo` extra"
        )


def test_demo_extra_carries_demo_deps():
    """A `demo` extra must exist and contain chromadb + sentence-transformers."""
    extras = _optional_dependencies()
    assert "demo" in extras, "missing `demo` optional-dependency extra"
    demo_names = _dep_names(extras["demo"])
    assert "chromadb" in demo_names, f"demo extra must contain chromadb, got: {extras['demo']!r}"
    assert "sentence-transformers" in demo_names, (
        f"demo extra must contain sentence-transformers, got: {extras['demo']!r}"
    )


def test_phoenix_extra_replaces_observability():
    """`observability` was renamed to `phoenix` (Phase 5); no `observability` remains."""
    extras = _optional_dependencies()
    assert "phoenix" in extras, "missing `phoenix` optional-dependency extra"
    assert "observability" not in extras, (
        "`observability` extra must be renamed to `phoenix`; no `observability` may remain"
    )
