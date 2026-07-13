"""Unit tests for the Tier 2 OpenAPI gate's classification logic.

The gate script lives in scripts/ (not a package), so it is loaded via
``importlib.util.spec_from_file_location``. All git/oasdiff interactions are
monkeypatched ``subprocess.run`` calls — no real git state is touched.
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_openapi_breaking.py"
_spec = importlib.util.spec_from_file_location("check_openapi_breaking", _SCRIPT_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_git(monkeypatch, *, inside_rc=0, head_rc=0, ls_tree_rc=0, ls_tree_out=None, show_rc=0):
    """Route the gate's git subcommands to canned exit codes/output."""
    if ls_tree_out is None:
        ls_tree_out = "services/orchestrator/openapi.json\n"

    def fake_run(cmd, **kwargs):
        joined = " ".join(cmd)
        if "--is-inside-work-tree" in joined:
            return _proc(inside_rc, stderr="fatal: not a git repository" if inside_rc else "")
        if "rev-parse --verify --quiet HEAD" in joined:
            return _proc(head_rc)
        if "ls-tree" in joined:
            return _proc(
                ls_tree_rc,
                stdout="" if ls_tree_rc else ls_tree_out,
                stderr="fatal: ls-tree broke" if ls_tree_rc else "",
            )
        if "show" in joined:
            return _proc(show_rc, stdout="{}", stderr="fatal: show broke" if show_rc else "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)


def test_not_a_repo_fails_closed(monkeypatch, capsys):
    """git cannot confirm a work tree → GateError → main() exits 1, never skips."""
    monkeypatch.setattr(gate.shutil, "which", lambda _: "/usr/bin/oasdiff")
    _patch_git(monkeypatch, inside_rc=128)

    assert gate.main() == 1
    assert "cannot verify a baseline" in capsys.readouterr().err


def test_unborn_head_passes_with_note(monkeypatch, capsys):
    """Repo's first commit (no HEAD yet) → legitimately no baseline → pass."""
    monkeypatch.setattr(gate.shutil, "which", lambda _: "/usr/bin/oasdiff")
    _patch_git(monkeypatch, head_rc=1)

    assert gate.main() == 0
    assert "unborn" in capsys.readouterr().out


def test_contract_absent_at_head_passes_with_note(monkeypatch, capsys):
    """ls-tree success with empty output = contract's first commit → pass."""
    monkeypatch.setattr(gate.shutil, "which", lambda _: "/usr/bin/oasdiff")
    _patch_git(monkeypatch, ls_tree_out="")

    assert gate.main() == 0
    assert "does not exist at HEAD" in capsys.readouterr().out


def test_ls_tree_unexpected_exit_fails_closed(monkeypatch):
    """A non-zero ls-tree exit is a git failure, never 'no baseline'."""
    _patch_git(monkeypatch, ls_tree_rc=128)

    with pytest.raises(gate.GateError, match="ls-tree"):
        gate.resolve_baseline()


def test_git_show_failure_after_ls_tree_success_fails_closed(monkeypatch):
    """The object exists at HEAD but cannot be read → fail closed."""
    _patch_git(monkeypatch, show_rc=128)

    with pytest.raises(gate.GateError, match="git show"):
        gate.resolve_baseline()


def test_oasdiff_exit_0_passes(monkeypatch):
    """oasdiff exit 0 → no breaking change → gate passes."""
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: _proc(0, stdout="ok\n"))

    assert gate.run_oasdiff("{}") == 0


def test_oasdiff_exit_1_is_a_breaking_change(monkeypatch, capsys):
    """oasdiff exit 1 → breaking change message, exit 1."""
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: _proc(1, stdout="error: removed endpoint\n")
    )

    assert gate.run_oasdiff("{}") == 1
    err = capsys.readouterr().err
    assert "breaking OpenAPI change" in err
    assert "tool error" not in err


def test_oasdiff_other_exit_is_a_tool_error_not_a_breaking_change(monkeypatch, capsys):
    """oasdiff exit 102 → distinct tool-error diagnosis (fail closed), exit 1."""
    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: _proc(102, stderr="failed to load spec\n")
    )

    assert gate.run_oasdiff("{}") == 1
    err = capsys.readouterr().err
    assert "oasdiff tool error (exit 102)" in err
    assert "failed to load spec" in err
    assert "breaking OpenAPI change" not in err
