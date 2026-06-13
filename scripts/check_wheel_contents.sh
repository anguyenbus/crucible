#!/usr/bin/env bash
# Phase 0 wheel-content verification (decision 0.0); Phase 5 single-sourced the
# contracts in-package and added parser_output, so this now asserts 4 schemas.
#
# Builds the crucible wheel with the configured build backend (uv_build) and
# asserts that the 4 in-package schema JSONs under src/crucible/contracts/ are
# present in the wheel. This guards against uv_build silently dropping the data
# files that hatchling shipped automatically.
#
# Scope: PR evidence only. This script is NOT wired into any CI gate this phase
# (the automated kernel-wheel-install job is Phase 6). Run it manually:
#
#     bash scripts/check_wheel_contents.sh
#
# Exits non-zero if uv build fails or if any of the 4 schema paths is missing.
#
# Phase 5: run-from-anywhere is now real. The kernel validator resolves schemas
# via importlib.resources against crucible.contracts (no CWD-relative paths;
# Finding A closed), and these JSONs ship in the wheel, so an installed wheel can
# validate without a top-level contracts/ dir.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

EXPECTED=(
    "crucible/contracts/eval_questions.schema.json"
    "crucible/contracts/legal_rag_bench_query_output.schema.json"
    "crucible/contracts/parser_output.schema.json"
    "crucible/contracts/rag_query_output.schema.json"
)

echo "[check_wheel_contents] building wheel with uv build..."
rm -rf dist
uv build --wheel

WHEEL="$(ls dist/*.whl 2>/dev/null | head -n1)"
if [[ -z "${WHEEL}" ]]; then
    echo "[check_wheel_contents] ERROR: no wheel was produced in dist/" >&2
    exit 1
fi
echo "[check_wheel_contents] inspecting ${WHEEL}"

LISTING="$(unzip -l "${WHEEL}")"
echo "${LISTING}"

MISSING=0
for path in "${EXPECTED[@]}"; do
    if echo "${LISTING}" | grep -qF "${path}"; then
        echo "[check_wheel_contents] OK   ${path}"
    else
        echo "[check_wheel_contents] FAIL ${path} is MISSING from the wheel" >&2
        MISSING=1
    fi
done

if [[ "${MISSING}" -ne 0 ]]; then
    echo "[check_wheel_contents] FAILED: one or more schema JSONs missing from the wheel" >&2
    exit 1
fi

echo "[check_wheel_contents] PASS: all 4 in-package schema JSONs present in the wheel"
