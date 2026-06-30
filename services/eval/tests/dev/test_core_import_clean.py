"""Phase 1 boundary enforcement: importing core pulls in no demo dependencies.

This is the REAL Phase 1 enforcement of the ``dev`` quarantine. The
import-linter contracts are vacuous in Phase 1 (``app.kernel`` /
``app`` service children do not exist yet), so this subprocess/``sys.modules`` check
is what actually proves the boundary holds: importing the core adapter path must
not transitively import the demo-only ``chromadb`` or ``sentence_transformers``
packages.

Follows the subprocess import-smoke style of ``tests/test_package.py``: a fresh
interpreter isolates ``sys.modules`` from the parent test process.
"""

import subprocess
import sys

# Core module path the quarantine protects. rag_adapter.py moves into kernel/ in
# Phase 2 and must never reach into dev.stubs.
_CORE_MODULE = "app.kernel.interfaces"

# Demo-only dependencies that must never be pulled in by a core import.
_FORBIDDEN_MODULES = ("chromadb", "sentence_transformers")


def test_core_import_pulls_in_no_demo_dependencies():
    """Importing the core adapter in a fresh subprocess imports no demo deps."""
    forbidden = ", ".join(repr(m) for m in _FORBIDDEN_MODULES)
    code = (
        "import importlib, sys\n"
        f"importlib.import_module({_CORE_MODULE!r})\n"
        f"forbidden = [{forbidden}]\n"
        "leaked = sorted(m for m in forbidden if m in sys.modules)\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked_line = next(line for line in result.stdout.splitlines() if line.startswith("LEAKED:"))
    leaked = [m for m in leaked_line.removeprefix("LEAKED:").split(",") if m]
    assert leaked == [], (
        f"core import of {_CORE_MODULE} leaked demo dependencies into sys.modules: "
        f"{leaked}. The dev quarantine has been breached."
    )
