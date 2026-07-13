#!/usr/bin/env bash
# Orchestrator stages grep-gate (parallel to eval's scripts/check_kernel_grep_gate.sh).
#
# import-linter's stages-pure contract graphs IMPORT statements but cannot see
# attribute access (os.environ) or dynamic reads. This grep-gate is the
# complementary guard for reproducibility: pipeline stages under
# services/orchestrator/app/orchestrator/ must never read the environment or
# load dotenv -- behavior comes ONLY from the pinned {name}-{semver} config.
#
# Deliberately a small parallel script rather than a parameterized rewrite of
# eval's gate: the forbidden sets differ (eval also grep-bans boto3/phoenix
# code use; the orchestrator bans those via import-linter's stages-pure
# contract) and eval's gate must keep behaving byte-identically.
#
# The patterns target real code forms (os.environ[/. , os.getenv, load_dotenv()
# so prose mentions in docstrings/comments (which legitimately DESCRIBE the
# purity rule) are not false-positives. Exits non-zero on any match.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

STAGES_DIR="services/orchestrator/app/orchestrator"
# Forbidden CODE usage (not prose mentions): attribute/subscript access and
# calls. Alternation (not a char class) for portability across grep/ugrep.
FORBIDDEN_REGEX='os\.environ\[|os\.environ\.|os\.getenv|load_dotenv\('

if [[ ! -d "$STAGES_DIR" ]]; then
    echo "[stages-grep-gate] FAIL: ${STAGES_DIR} directory not found" >&2
    exit 1
fi

MATCHES="$(grep -rnE "$FORBIDDEN_REGEX" "$STAGES_DIR" --include='*.py' 2>/dev/null || true)"

if [[ -n "$MATCHES" ]]; then
    echo "[stages-grep-gate] FAIL: forbidden environment access in ${STAGES_DIR}:" >&2
    echo "$MATCHES" >&2
    echo "" >&2
    echo "[stages-grep-gate] Pipeline stages must stay env-free: behavior comes" >&2
    echo "ONLY from the pinned {name}-{semver} config; endpoints are injected" >&2
    echo "via app.config.Settings, never read inside a stage." >&2
    exit 1
fi

echo "[stages-grep-gate] PASS: no forbidden environment access in ${STAGES_DIR}"
