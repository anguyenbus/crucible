#!/usr/bin/env bash
# Phase 2 clean-venv kernel acceptance (decision Q8).
#
# Proves crucible.kernel is import-pure with ZERO extras: builds a fresh venv,
# installs ONLY the core dependencies (no optional extras, no dev group) plus
# pytest, then runs tests/kernel/. If the kernel ever reaches for an extra-only
# dependency (boto3, arize-phoenix, fastapi, scipy-via-extra, ...) the kernel
# tests fail to import here even though the full dev env would hide it.
#
# Scope: PR evidence + manual gate. This is NOT a per-commit pre-commit hook
# (a fresh resolve+install is too slow for every commit). Run it manually:
#
#     bash scripts/check_kernel_clean.sh
#
# Mirrors the Phase 0 scripts/check_wheel_contents.sh pattern. Exits non-zero if
# the venv build, the core install, or the kernel tests fail.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$(mktemp -d)/kernel-clean-venv"
trap 'rm -rf "$(dirname "$VENV_DIR")"' EXIT

echo "[check_kernel_clean] creating a fresh venv at ${VENV_DIR}"
uv venv "$VENV_DIR"

echo "[check_kernel_clean] installing crucible CORE ONLY (no extras, no dev) + pytest"
# --no-editable + no extras: only [project.dependencies] resolve. pytest is the
# only test-runner addition; it is NOT an extra-only dependency of crucible.
VIRTUAL_ENV="$VENV_DIR" uv pip install --python "$VENV_DIR/bin/python" . pytest

echo "[check_kernel_clean] running tests/kernel/ with the zero-extras interpreter"
# CRUCIBLE_GENERATOR_MODEL is set so the judge != generator invariant resolves
# without Bedrock (the kernel tests are fully deterministic and mocked anyway).
CRUCIBLE_GENERATOR_MODEL=gpt-4o "$VENV_DIR/bin/python" -m pytest -q tests/kernel/

echo "[check_kernel_clean] PASS: tests/kernel/ green in a zero-extras venv"
