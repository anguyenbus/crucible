#!/usr/bin/env bash
# Phase 2 kernel grep-gate (decision Q8).
#
# import-linter graphs IMPORT statements but cannot see attribute access
# (os.environ) or dynamic reads. This grep-gate is the complementary guard: it
# forbids forbidden CODE usage anywhere under services/eval/app/kernel/ --
# os.environ access, os.getenv, load_dotenv(), boto3 import/use, and phoenix
# import/use -- EXCEPT the single allowlisted DeepEval telemetry opt-out line in
# kernel/rag_metrics/__init__.py.
#
# The patterns target real code forms (os.environ[/. , import boto3, phoenix. ,
# from phoenix ...) so that prose mentions of these names inside docstrings and
# comments (which legitimately DESCRIBE the purity rule) are not false-positives.
#
# Wired as a `local` pre-commit hook (always_run). Exits non-zero on any
# non-allowlisted match.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

KERNEL_DIR="services/eval/app/kernel"
# The exact allowlisted line (the one permitted kernel os.environ write).
ALLOWLIST_REGEX='os\.environ\["DEEPEVAL_TELEMETRY_OPT_OUT"\] = "YES"'
# Forbidden CODE usage (not prose mentions): attribute/subscript access and
# import statements. Alternation (not a char class) for portability across
# grep/ugrep. `os\.environ\[` = subscript; `os\.environ\.` = attribute access.
FORBIDDEN_REGEX='os\.environ\[|os\.environ\.|os\.getenv|load_dotenv\(|import boto3|from boto3|boto3\.|import phoenix|from phoenix|phoenix\.'

if [[ ! -d "$KERNEL_DIR" ]]; then
    echo "[kernel-grep-gate] no ${KERNEL_DIR} directory; nothing to check"
    exit 0
fi

# grep -n every forbidden token, then drop the single allowlisted telemetry line.
MATCHES="$(grep -rnE "$FORBIDDEN_REGEX" "$KERNEL_DIR" --include='*.py' 2>/dev/null \
    | grep -vE "$ALLOWLIST_REGEX" || true)"

if [[ -n "$MATCHES" ]]; then
    echo "[kernel-grep-gate] FAIL: forbidden code usage in ${KERNEL_DIR}:" >&2
    echo "$MATCHES" >&2
    echo "" >&2
    echo "[kernel-grep-gate] The kernel must stay infra-free. Only the DeepEval" >&2
    echo "telemetry opt-out line in kernel/rag_metrics/__init__.py is allowed." >&2
    exit 1
fi

echo "[kernel-grep-gate] PASS: no forbidden code usage in ${KERNEL_DIR} (telemetry line allowlisted)"
