"""Phase 4 SECURITY regression: kernel telemetry opt-out ordering.

The DeepEval telemetry opt-out (``DEEPEVAL_TELEMETRY_OPT_OUT=YES``) MUST be set
before ``deepeval`` is ever imported, or telemetry can leak sensitive query data
to external servers. ``app.kernel.rag_metrics`` is one of the two allowlisted
opt-out sites; this test proves the invariant on the kernel path.

Each assertion runs in a FRESH interpreter (``subprocess``) so ``sys.modules`` and
``os.environ`` are isolated from the parent pytest process. Follows the
subprocess import-smoke style of ``tests/local/test_core_import_clean.py``.
"""

import subprocess
import sys


def test_kernel_rag_metrics_sets_optout_before_deepeval() -> None:
    """Importing the kernel rag_metrics package sets the opt-out before deepeval.

    Fresh interpreter: assert ``deepeval`` is absent, import the package, then
    assert (1) the opt-out is ``"YES"`` and (2) ``deepeval`` is STILL absent from
    ``sys.modules`` -- proving the env write fires at the package ``__init__``,
    structurally before any (lazy) deepeval import the package may later trigger.
    """
    code = (
        "import os, sys\n"
        "os.environ.pop('DEEPEVAL_TELEMETRY_OPT_OUT', None)\n"
        "assert 'deepeval' not in sys.modules, 'deepeval imported too early'\n"
        "import app.kernel.rag_metrics  # noqa: F401\n"
        "optout = os.environ.get('DEEPEVAL_TELEMETRY_OPT_OUT')\n"
        "deepeval_loaded = 'deepeval' in sys.modules\n"
        "print(f'OPTOUT={optout}')\n"
        "print(f'DEEPEVAL_LOADED={deepeval_loaded}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    optout = next(line for line in lines if line.startswith("OPTOUT=")).removeprefix("OPTOUT=")
    deepeval_loaded = next(
        line for line in lines if line.startswith("DEEPEVAL_LOADED=")
    ).removeprefix("DEEPEVAL_LOADED=")

    assert optout == "YES", (
        f"kernel.rag_metrics did not set DEEPEVAL_TELEMETRY_OPT_OUT=YES (got {optout!r}); "
        "the telemetry opt-out SECURITY invariant is broken."
    )
    assert deepeval_loaded == "False", (
        "deepeval was imported by app.kernel.rag_metrics before the opt-out could "
        "be guaranteed; the opt-out must precede any deepeval import."
    )
