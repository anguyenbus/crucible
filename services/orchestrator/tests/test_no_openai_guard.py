"""Group 9 gap-fill — Bedrock-only / no-OpenAI invariant for the ORCHESTRATOR.

The guardrail pod (``services/guardrail/tests/test_no_openai_guard.py``) and the
eval service (``services/eval/tests/deepeval/test_no_openai_guard.py``) each hold
a no-OpenAI hard gate. The orchestrator now pulls ``langchain-aws`` transitively
(the ``/compare`` decompose-then-verify judge builds a ``ChatBedrockConverse``),
so the SAME invariant must be pinned HERE — otherwise a drifted dependency (or a
stray ``langchain-openai``) could reintroduce an OpenAI client on the answer path.
``openai`` is importable in the shared workspace venv (deepeval pulls it), so the
lockfile audit — not import availability — is the load-bearing guard.

Two checks, mirroring the pod gate:

1. Lockfile audit: ``openai`` / ``langchain-openai`` are ABSENT from the resolved
   orchestrator ``uv.lock``; ``langchain-aws`` (the Bedrock path) IS present.
2. End-to-end construction: with ``openai.OpenAI`` / ``openai.AsyncOpenAI``
   monkeypatched to RAISE, the production ``/compare`` model-build seam
   (``_build_chat_model``) constructs a ``ChatBedrockConverse`` and never touches
   the OpenAI sentinel. Construction is offline (binds a boto3 client; no call).
"""

from __future__ import annotations

import sys
import tomllib
import types
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_LOCK_PATH = _SERVICE_ROOT / "uv.lock"

# A pinned Anthropic-on-Bedrock generator id (any valid pin — construction binds
# the boto3 client lazily and makes NO network call).
_BEDROCK_MODEL_ID = "au.anthropic.claude-sonnet-4-5-20250929-v1:0"


def _boom_openai(*_: object, **__: object):  # pragma: no cover - must never run
    raise AssertionError(
        "An OpenAI client was constructed in the orchestrator. The no-OpenAI "
        "invariant FAILED: an OpenAI path reappeared on the Bedrock-only answer "
        "path. Verify langchain-openai / an openai-pulling dep did not resolve "
        "into services/orchestrator/uv.lock."
    )


def test_openai_absent_from_resolved_orchestrator_lock():
    """Lockfile audit: no `openai` / `langchain-openai` in the resolved uv.lock."""
    data = tomllib.loads(_LOCK_PATH.read_text())
    names = {pkg.get("name") for pkg in data.get("package", [])}

    assert "openai" not in names, (
        "`openai` resolved into services/orchestrator/uv.lock — an "
        "openai-pulling dependency crept in. Bedrock-only is acceptance-blocking."
    )
    assert "langchain-openai" not in names, (
        "`langchain-openai` resolved into services/orchestrator/uv.lock — the "
        "compare judge must stay on langchain-aws / ChatBedrockConverse."
    )
    # The Bedrock path we DO depend on is present (guards against silently
    # dropping the langchain-aws pin this invariant is asserted against).
    assert "langchain-aws" in names


def test_compare_model_build_seam_is_chatbedrockconverse_never_openai(monkeypatch):
    """The `/compare` judge/generator build seam constructs ChatBedrockConverse."""
    # Inject a sentinel `openai` module whose clients RAISE, so ANY accidental
    # OpenAI construction fails loudly on the compare model-build path.
    sentinel = types.ModuleType("openai")
    sentinel.OpenAI = _boom_openai  # type: ignore[attr-defined]
    sentinel.AsyncOpenAI = _boom_openai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", sentinel)

    from app.routers.compare import _build_chat_model

    # Real construction through the production seam (offline: no Bedrock call).
    model = _build_chat_model(_BEDROCK_MODEL_ID, "ap-southeast-2", 512)

    # The inner model is the Bedrock Converse client — never an OpenAI client.
    assert type(model).__name__ == "ChatBedrockConverse"
    assert model.model_id == _BEDROCK_MODEL_ID
