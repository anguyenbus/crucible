"""Scaffold tests: import purity and typed pipeline stage modules.

Focused checks only: the app imports with NO configured environment, and the
``app/orchestrator/`` stage modules carry Phase 2 typed signatures (the
Phase 1 ``run(payload)`` mega-dict is deleted). Exhaustive directory-content
tests are intentionally skipped.
"""

import importlib
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]

# The 8 pipeline stage modules mandated by the genai-backend module vocabulary
# (docs/eval_final/8-gen-ai-repo-tree.md). All are typed pure functions in
# Phase 2 (live logic or typed identities for the parked stages).
STAGE_MODULES = [
    "app.orchestrator.policy_router",
    "app.orchestrator.query_rewrite",
    "app.orchestrator.retriever",
    "app.orchestrator.reranker",
    "app.orchestrator.context_assembler",
    "app.orchestrator.prompt_builder",
    "app.orchestrator.guardrails",
    "app.orchestrator.citation_builder",
]


def test_import_app_main_requires_no_environment():
    """``import app.main`` succeeds in a subprocess with NO env vars set.

    Proves there are no import-time side effects that depend on a configured
    environment (eval's discipline: importing never requires env vars).
    """
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=SERVICE_ROOT,
        env={"PYTHONPATH": str(SERVICE_ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"`import app.main` failed with a bare environment:\n{result.stderr}"
    )


def test_stage_modules_no_longer_expose_the_run_mega_dict():
    """Phase 2: the uniform ``run(payload)`` signature is DELETED from every stage."""
    for module_name in STAGE_MODULES:
        module = importlib.import_module(module_name)
        assert not hasattr(module, "run"), (
            f"{module_name} must not expose the Phase 1 `run(payload)` mega-dict "
            "signature — stages carry explicit typed signatures in Phase 2"
        )


def test_policy_router_docstring_flags_naming_hazard():
    """policy_router documents that it is NOT the external policy/authz service."""
    policy_router = importlib.import_module("app.orchestrator.policy_router")
    doc = policy_router.__doc__ or ""
    assert "NOT the external policy" in doc, (
        "policy_router.py's docstring must flag the naming hazard: it is the "
        "pipeline-routing stage (direct/RAG/tool), NOT the external "
        "policy/authorization service"
    )
