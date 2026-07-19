"""Task Group 1.7: the ACTUAL network-blocked OFFLINE-STARTUP smoke test.

This is a real, verifiable test — it shells out to `scripts/run_offline_smoke.sh`,
which builds the image and runs it with `--network none`, confirming the service
starts and a trivial parse succeeds using ONLY baked models (not "a file copied").

It is marked `offline_smoke` and is NOT part of the default unit run. It SKIPS
(never fabricates a pass) when Docker is unavailable in the environment — the
harness returns exit code 3 for that case, which this test maps to pytest.skip.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parent.parent / "scripts" / "run_offline_smoke.sh"


@pytest.mark.offline_smoke
def test_offline_startup_smoke():
    if shutil.which("docker") is None:
        pytest.skip("docker not installed — cannot run the network-blocked offline smoke test")

    proc = subprocess.run(
        ["bash", str(_HARNESS)],
        capture_output=True,
        text=True,
    )
    # Exit 3 = docker present but daemon unusable → honest skip, never a fake pass.
    if proc.returncode == 3:
        pytest.skip(f"docker unavailable/unusable: {proc.stderr.strip()}")

    assert proc.returncode == 0, (
        f"offline-startup smoke FAILED (exit {proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "PASS" in proc.stdout
