"""Phase 4 SECURITY regression: service opt-out ordering + zero import-time mutation.

Two invariants are proven here, each in a FRESH interpreter (``subprocess``) so
``sys.modules`` / ``os.environ`` are isolated from the parent pytest process:

1. ``crucible.service.deepeval`` (the second allowlisted opt-out site) sets
   ``DEEPEVAL_TELEMETRY_OPT_OUT=YES`` before any deepeval import. The opt-out is a
   SECURITY invariant: deepeval phones home unless it is set before deepeval runs,
   and evaluation runs may carry sensitive query data.

2. ``import crucible`` mutates ZERO ``os.environ`` keys. This holds ONLY because
   Phase 4 (Commit 1) deleted the top-level opt-out write at ``crucible/__init__.py``.
   Authored before that deletion, the zero-mutation test would FAIL (the top-level
   write would add ``DEEPEVAL_TELEMETRY_OPT_OUT``) -- that ordering is the proof the
   deletion was load-bearing, not cosmetic.

Follows the subprocess import-smoke style of ``tests/local/test_core_import_clean.py``.
"""

import json
import subprocess
import sys


def test_service_deepeval_sets_optout_before_deepeval() -> None:
    """Importing crucible.service.deepeval sets the opt-out before deepeval.

    Fresh interpreter: assert ``deepeval`` is absent, import the package, then
    assert (1) the opt-out is ``"YES"`` and (2) ``deepeval`` is STILL absent from
    ``sys.modules`` -- proving the env write fires at the package ``__init__``,
    structurally before bedrock_provider's lazy deepeval imports.
    """
    code = (
        "import os, sys\n"
        "os.environ.pop('DEEPEVAL_TELEMETRY_OPT_OUT', None)\n"
        "assert 'deepeval' not in sys.modules, 'deepeval imported too early'\n"
        "import crucible.service.deepeval  # noqa: F401\n"
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
        f"crucible.service.deepeval did not set DEEPEVAL_TELEMETRY_OPT_OUT=YES (got {optout!r}); "
        "the telemetry opt-out SECURITY invariant is broken."
    )
    assert deepeval_loaded == "False", (
        "deepeval was imported by crucible.service.deepeval before the opt-out could "
        "be guaranteed; the opt-out must precede any deepeval import."
    )


def test_phoenix_evaluators_sets_optout_before_deepeval() -> None:
    """Importing crucible.service.phoenix.evaluators sets the opt-out first.

    ``evaluators.py`` imports ``deepeval.metrics`` lazily but is NOT inside an
    allowlisted opt-out package; it carries a top-level ``import
    crucible.service.deepeval`` guard so the opt-out fires structurally (not by
    caller ordering) before any lazy deepeval import. Regression for the residual
    third-path gap closed in Phase 4. Fresh interpreter: assert deepeval absent,
    import evaluators, assert opt-out is ``"YES"``.
    """
    code = (
        "import os, sys\n"
        "os.environ.pop('DEEPEVAL_TELEMETRY_OPT_OUT', None)\n"
        "assert 'deepeval' not in sys.modules, 'deepeval imported too early'\n"
        "import crucible.service.phoenix.evaluators  # noqa: F401\n"
        "print('OPTOUT=' + str(os.environ.get('DEEPEVAL_TELEMETRY_OPT_OUT')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    optout = next(
        line for line in result.stdout.splitlines() if line.startswith("OPTOUT=")
    ).removeprefix("OPTOUT=")
    assert optout == "YES", (
        "importing crucible.service.phoenix.evaluators did not set "
        f"DEEPEVAL_TELEMETRY_OPT_OUT=YES (got {optout!r}); the third-path telemetry "
        "opt-out guard is broken."
    )


def test_import_crucible_mutates_no_environ() -> None:
    """``import crucible`` adds or changes ZERO os.environ keys.

    Fresh interpreter: snapshot ``os.environ`` before and after ``import crucible``
    and assert no key was added or changed -- in particular NO
    ``DEEPEVAL_TELEMETRY_OPT_OUT`` and no dotenv-sourced vars. This passes only
    because Phase 4 Commit 1 deleted the top-level opt-out write at
    ``crucible/__init__.py:34`` and Commit 2 confined ``load_dotenv()``; before
    those changes ``import crucible`` mutated the environment.
    """
    code = (
        "import os, json\n"
        "before = dict(os.environ)\n"
        "import crucible  # noqa: F401\n"
        "after = dict(os.environ)\n"
        "added = sorted(k for k in after if k not in before)\n"
        "changed = sorted(k for k in after if k in before and after[k] != before[k])\n"
        "print('ADDED=' + json.dumps(added))\n"
        "print('CHANGED=' + json.dumps(changed))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    added = json.loads(
        next(line for line in lines if line.startswith("ADDED=")).removeprefix("ADDED=")
    )
    changed = json.loads(
        next(line for line in lines if line.startswith("CHANGED=")).removeprefix("CHANGED=")
    )

    assert added == [], (
        f"import crucible added os.environ keys {added}; library imports must mutate "
        "no global state (Phase 4 invariant)."
    )
    assert changed == [], (
        f"import crucible changed os.environ keys {changed}; library imports must mutate "
        "no global state (Phase 4 invariant)."
    )
