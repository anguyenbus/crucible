"""Thin `requires_aws` wrappers around the Phase 0 verification scripts.

Each script is the actual smoke test (pass/fail, exits 0/1); these wrappers
just run them in a subprocess and assert exit code 0. They skip cleanly when
AWS credentials are absent (see conftest.py).
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def run_check_script(script_name: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"{script_name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.requires_aws
def test_check_connectivity_passes():
    run_check_script("check_connectivity.py")


@pytest.mark.requires_aws
def test_check_titan_passes():
    run_check_script("check_titan.py")


@pytest.mark.requires_aws
def test_check_search_pipeline_passes():
    run_check_script("check_search_pipeline.py")
