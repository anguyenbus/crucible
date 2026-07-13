"""
Tier 2 OpenAPI gate: classify committed contract diffs with ``oasdiff``.

Runs ``oasdiff breaking`` comparing the git baseline
(``git show HEAD:services/orchestrator/openapi.json``) against the working-tree
``openapi.json``, failing the commit on breaking (ERR-level) changes. Policy:

- If the ``oasdiff`` binary is missing, this gate HARD-FAILS with a one-line
  install command — it must NEVER silently skip (the repo has no CI; pre-commit
  is the only gate). No Docker-wrapped oasdiff: commits must not depend on a
  Docker daemon.
- Git errors FAIL CLOSED: if git cannot confirm whether a baseline exists
  (not a repo, unexpected ``ls-tree`` failure, ``git show`` failure after the
  path was confirmed to exist at HEAD), the gate exits 1 rather than
  silently skipping.
- The gate passes with a note ONLY in the two provably-baseline-free states:
  an unborn HEAD (the repo's first commit) or a contract that does not exist
  at HEAD yet (the contract's first commit).
- oasdiff exit codes are classified: 0 = pass, 1 = breaking change, anything
  else = oasdiff tool error (fail closed, diagnosed as a tool error — never
  mislabelled as a breaking change).

Pure standard library; the only external requirements are ``git`` and the
``oasdiff`` Go binary on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

SERVICE_DIR: Final[Path] = Path(__file__).resolve().parents[1]
REPO_ROOT: Final[Path] = SERVICE_DIR.parents[1]
# Repo-relative spec path, as git show needs it.
SPEC_REPO_PATH: Final[str] = "services/orchestrator/openapi.json"
INSTALL_HINT: Final[str] = "go install github.com/oasdiff/oasdiff@latest"


class GateError(Exception):
    """The gate cannot verify a baseline — FAIL CLOSED (exit 1), never skip."""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command against the repo root, capturing output."""
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
    )


def resolve_baseline() -> str | None:
    """
    Return the baseline spec text from HEAD, or ``None`` if none can exist.

    ``None`` is returned ONLY for the two provably-baseline-free states
    (unborn HEAD; contract absent at HEAD). Every other git failure raises
    :class:`GateError` so the gate fails closed instead of silently skipping.
    """
    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        raise GateError(
            "git cannot confirm this is a work tree — the Tier 2 gate cannot "
            "verify a baseline and never silently skips (fail closed).\n"
            f"git rev-parse stderr: {inside.stderr.strip()}"
        )

    head = _git("rev-parse", "--verify", "--quiet", "HEAD")
    if head.returncode != 0:
        # Unborn HEAD: the repo's very first commit — no baseline can exist.
        print("No baseline: HEAD is unborn (repo's first commit) — nothing to classify.")
        return None

    # ls-tree separates "path absent from the HEAD tree" (exit 0, empty
    # output) from git failures (non-zero exit) deterministically — unlike
    # `cat-file -e HEAD:<path>`, which exits 128 for BOTH a missing path and
    # real git errors, making the first-commit-of-the-contract case
    # indistinguishable from a failure.
    listed = _git("ls-tree", "--name-only", "HEAD", "--", SPEC_REPO_PATH)
    if listed.returncode != 0:
        raise GateError(
            f"git ls-tree HEAD -- {SPEC_REPO_PATH} failed unexpectedly "
            f"(exit {listed.returncode}) — cannot verify the baseline (fail "
            f"closed).\ngit stderr: {listed.stderr.strip()}"
        )
    if not listed.stdout.strip():
        print(
            f"No baseline: {SPEC_REPO_PATH} does not exist at HEAD (first commit "
            f"of the contract) — nothing to classify as breaking."
        )
        return None

    baseline = _git("show", f"HEAD:{SPEC_REPO_PATH}")
    if baseline.returncode != 0:
        raise GateError(
            f"git show HEAD:{SPEC_REPO_PATH} failed (exit {baseline.returncode}) "
            f"even though the object exists at HEAD — cannot read the baseline "
            f"(fail closed).\ngit stderr: {baseline.stderr.strip()}"
        )
    return baseline.stdout


def run_oasdiff(baseline_text: str) -> int:
    """
    Run ``oasdiff breaking`` against the baseline and classify its exit code.

    Returns the gate exit code: 0 = no breaking change; 1 = breaking change
    OR oasdiff tool error (fail closed, but each diagnosed distinctly).
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".openapi-baseline.json", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(baseline_text)
        baseline_path = Path(handle.name)

    try:
        result = subprocess.run(
            [
                "oasdiff",
                "breaking",
                str(baseline_path),
                str(SERVICE_DIR / "openapi.json"),
                "--fail-on",
                "ERR",
            ],
            capture_output=True,
            text=True,
        )
    finally:
        baseline_path.unlink(missing_ok=True)

    if result.stdout:
        print(result.stdout, end="")

    if result.returncode == 0:
        return 0
    if result.returncode == 1:
        # oasdiff's documented "breaking changes found" exit code.
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        print(
            "ERROR: breaking OpenAPI change vs the HEAD baseline (see oasdiff "
            "output above). Breaking the /query contract requires a deliberate, "
            "coordinated change with the eval consumer.",
            file=sys.stderr,
        )
        return 1
    # Any other exit code is NOT a classified breaking change — it means
    # oasdiff itself failed (bad flags, unreadable spec, internal error).
    # Fail closed, but diagnose it correctly for the operator.
    print(
        f"ERROR: oasdiff tool error (exit {result.returncode}) — the gate could "
        f"not classify the diff; this is NOT a detected breaking change (fail "
        f"closed).\noasdiff stderr: {result.stderr.strip()}",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    """Classify openapi.json changes vs the HEAD baseline; fail on breaking."""
    if shutil.which("oasdiff") is None:
        print(
            "ERROR: 'oasdiff' binary not found on PATH — the Tier 2 OpenAPI "
            "breaking-change gate never silently skips.\n"
            f"Install it with: {INSTALL_HINT}",
            file=sys.stderr,
        )
        return 1

    try:
        baseline_text = resolve_baseline()
    except GateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if baseline_text is None:
        return 0
    return run_oasdiff(baseline_text)


if __name__ == "__main__":
    raise SystemExit(main())
