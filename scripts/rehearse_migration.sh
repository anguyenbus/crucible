#!/usr/bin/env bash
# Phase 6 migration rehearsal -- the binding guarantee.
#
# Proves continuously and MECHANICALLY that migrating crucible into the monorepo
# is a directory copy plus a single mechanical import rewrite. It copies the three
# migrating buckets (kernel/, the service children, contracts/) into a fresh app/
# package, applies the four import-rewrite rules, ASSERTS zero surviving dotted
# `crucible.` reference and no `import crucible` statement, resolves against the
# scaffold's own committed lockfile, import-smokes every app.* module, and runs the
# mirrored test suite. "Green end-to-end" is the migration proven by construction.
#
# Scope: PR evidence + required pre-merge gate (alongside check_kernel_clean.sh
# et al.). NOT wired into pre-commit: no CI exists and the venv build is slow.
# Run it manually:
#
#     bash scripts/rehearse_migration.sh [DEST]
#
# DEST defaults to services/eval (crucible's single migration scaffold; only its
# committed pyproject.toml/uv.lock/app/__init__.py seed the build -- the generated
# app/ + tests are gitignored). The script is idempotent: it rm -rf's the generated
# app/ subdirs + copied tests first, so a re-run is a clean rebuild.
#
# Known pre-existing flake: test_chromadb_collection_exists is in tests/local (NOT
# copied), so it cannot appear here. The rehearsal suite must be fully green.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DEST="${1:-services/eval}"
# Self-contained: the judge != generator invariant resolves without Bedrock.
export CRUCIBLE_GENERATOR_MODEL=gpt-4o

SRC="src/crucible"
SERVICE_CHILDREN=(deepeval phoenix datasets metrics runners)

echo "[rehearse] REPO_ROOT=${REPO_ROOT}"
echo "[rehearse] DEST=${DEST}"

if [[ ! -f "${DEST}/pyproject.toml" || ! -f "${DEST}/uv.lock" ]]; then
    echo "[rehearse] ERROR: ${DEST} is not a locked scaffold (missing pyproject.toml or uv.lock)" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Idempotency: wipe everything the script generates so a re-run is a clean build.
# Keeps the committed scaffold (pyproject.toml, uv.lock, app/__init__.py,
# tests/.gitkeep, .gitignore) untouched.
# ---------------------------------------------------------------------------
echo "[rehearse] (idempotent) removing previously generated contents under ${DEST}"
rm -rf \
    "${DEST}/app/kernel" \
    "${DEST}/app/contracts" \
    "${DEST}/app/config.py"
for child in "${SERVICE_CHILDREN[@]}"; do
    rm -rf "${DEST}/app/${child}"
done
rm -rf \
    "${DEST}/tests/kernel" \
    "${DEST}/tests/service" \
    "${DEST}/tests/conftest.py"
# Drop any stale bytecode caches under the generated tree.
find "${DEST}" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# (a) Copy the three buckets + config into app/.
#     - kernel/ copies whole to app/kernel/
#     - each service child -> app/<child>/
#     - service/config.py -> app/config.py
#     - service/__init__.py is deliberately NOT copied (no app/service/ package)
#     - contracts/ -> app/contracts/
# ---------------------------------------------------------------------------
echo "[rehearse] (a) copying kernel/, service children, config.py, contracts/ into ${DEST}/app/"
mkdir -p "${DEST}/app"
cp -r "${SRC}/kernel" "${DEST}/app/kernel"
for child in "${SERVICE_CHILDREN[@]}"; do
    cp -r "${SRC}/service/${child}" "${DEST}/app/${child}"
done
cp "${SRC}/service/config.py" "${DEST}/app/config.py"
cp -r "${SRC}/contracts" "${DEST}/app/contracts"

# Strip any copied bytecode caches (they reference the old crucible.* paths).
find "${DEST}/app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# (b) Copy the mirrored test subset + the phoenix_integration auto-skip conftest.
#     - tests/kernel + tests/service -> $DEST/tests/
#     - tests/conftest.py -> $DEST/tests/conftest.py
#     - tests/local (demo) and tests/test_dependency_contract.py (crucible-root-
#       anchored) are deliberately NOT copied.
# ---------------------------------------------------------------------------
echo "[rehearse] (b) copying tests/kernel, tests/service, tests/conftest.py into ${DEST}/tests/"
mkdir -p "${DEST}/tests"
cp -r tests/kernel "${DEST}/tests/kernel"
cp -r tests/service "${DEST}/tests/service"
cp tests/conftest.py "${DEST}/tests/conftest.py"
find "${DEST}/tests" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# (c) The mechanical import rewrite. Applied across every copied .py file.
#
#     The FOUR rewrite rules (ORDER MATTERS -- dotted rules first):
#       1. crucible.kernel      -> app.kernel
#       2. crucible.service.    -> app.            (children flatten into app/)
#       3. crucible.contracts   -> app.contracts
#       4. from crucible.service import -> from app import
#          from crucible import          -> from app import   (defensive bare form)
#
#     Rule 1 runs BEFORE rule 2 so `crucible.kernel` is never mangled by the
#     `crucible.service.` -> `app.` substitution; rule 2 carries a trailing dot so
#     it only rewrites dotted service children (crucible.service.deepeval ->
#     app.deepeval) and the bare `crucible.service` token (none survive here).
#
#     Top-level-package form (sibling of rule 4's `from crucible import`): the bare
#     statement `import crucible` is the top-level package whose monorepo analog is
#     `import app`. tests/service/test_telemetry_optout.py asserts that importing
#     the top-level package mutates ZERO os.environ keys; that invariant carries
#     over verbatim as `import app` (app/__init__.py is import-pure). We rewrite the
#     bare `import crucible` statement (and its prose mentions) to `import app` so
#     the invariant is preserved and no `import crucible` statement survives.
# ---------------------------------------------------------------------------
echo "[rehearse] (c) applying the four import-rewrite rules across all copied .py"
mapfile -t PY_FILES < <(find "${DEST}/app" "${DEST}/tests" -name '*.py' -type f)
for f in "${PY_FILES[@]}"; do
    sed -i \
        -e 's/crucible\.kernel/app.kernel/g' \
        -e 's/crucible\.service\./app./g' \
        -e 's/crucible\.contracts/app.contracts/g' \
        -e 's/from crucible\.service import/from app import/g' \
        -e 's/from crucible import/from app import/g' \
        -e 's/import crucible\b/import app/g' \
        "$f"
done

# ---------------------------------------------------------------------------
# (d) Zero-reference assertion (FATAL on match).
#     - No dotted `crucible.` may survive (import-/module-path shaped survivors).
#     - No `import crucible` / `from crucible ` statement may survive.
#     The dot allows the proper noun "Crucible" and slash-paths (crucible/...) in
#     prose; any genuine cross-bucket import or un-rewritable module path is a
#     FATAL leak (the rehearsal is designed to expose these).
# ---------------------------------------------------------------------------
echo "[rehearse] (d) asserting zero surviving crucible. references"
LEAK=0
if grep -rn 'crucible\.' "${DEST}/app" "${DEST}/tests"; then
    echo "[rehearse] FATAL: dotted 'crucible.' references survived the rewrite (above)" >&2
    LEAK=1
fi
if grep -rnE '(^|[^.[:alnum:]_])import crucible([^.[:alnum:]_]|$)|from crucible ' "${DEST}/app" "${DEST}/tests"; then
    echo "[rehearse] FATAL: an 'import crucible' / 'from crucible ' statement survived (above)" >&2
    LEAK=1
fi
if [[ "${LEAK}" -ne 0 ]]; then
    echo "[rehearse] FAILED: the migration rewrite is INCOMPLETE -- fix the leak above" >&2
    exit 1
fi
echo "[rehearse] OK: zero dotted crucible. and no import crucible statement"

# ---------------------------------------------------------------------------
# (e) Resolve against the scaffold's committed lock (validates deps + uv_build
#     packaging of app/contracts/*.json).
# ---------------------------------------------------------------------------
echo "[rehearse] (e) uv sync --frozen --extra bedrock --extra phoenix in ${DEST}"
uv sync --frozen --extra bedrock --extra phoenix --project "${DEST}"

VENV_PY="${DEST}/.venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
    echo "[rehearse] ERROR: scaffold venv python not found at ${VENV_PY}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# (f) Import-smoke every app.* module via pkgutil.walk_packages. Confirms the
#     kernel validator resolves app.contracts via importlib.resources in the
#     scaffold venv after the rewrite. A genuinely-optional-extra ImportError is
#     tolerated (skipped + reported); anything else is FATAL.
# ---------------------------------------------------------------------------
echo "[rehearse] (f) import-smoking every app.* module in the scaffold venv"
( cd "${DEST}" && CRUCIBLE_GENERATOR_MODEL=gpt-4o ./.venv/bin/python - <<'PYSMOKE'
import importlib
import pkgutil
import sys

import app

OPTIONAL_HINTS = ("chromadb", "sentence_transformers", "sentence-transformers", "datasets", "huggingface", "rich", "dotenv")

imported = 0
skipped = []
for mod in pkgutil.walk_packages(app.__path__, prefix="app."):
    name = mod.name
    try:
        importlib.import_module(name)
        imported += 1
    except ImportError as exc:  # noqa: PERF203
        msg = str(exc).lower()
        if any(h in msg for h in OPTIONAL_HINTS):
            skipped.append((name, str(exc)))
            continue
        print(f"[rehearse] FATAL import-smoke failure: {name}: {exc}", file=sys.stderr)
        raise

print(f"[rehearse] import-smoke imported {imported} app.* modules; skipped {len(skipped)} optional-extra modules")
for name, exc in skipped:
    print(f"[rehearse]   skipped (optional extra): {name}: {exc}")
PYSMOKE
)

# ---------------------------------------------------------------------------
# (g) Run the mirrored suite in the destination venv.
# ---------------------------------------------------------------------------
echo "[rehearse] (g) running the mirrored suite (pytest -m 'not phoenix_integration') in ${DEST}"
( cd "${DEST}" && CRUCIBLE_GENERATOR_MODEL=gpt-4o ./.venv/bin/python -m pytest -m "not phoenix_integration" )

echo "[rehearse] PASS: migration rehearsal GREEN end-to-end -- the migration is proven by construction."
