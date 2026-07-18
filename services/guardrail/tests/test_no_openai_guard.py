"""HARD CI GATE (I1): no OpenAI client is ever constructed on this Bedrock pod.

Mirrors ``services/eval/tests/deepeval/test_no_openai_guard.py`` for the
guardrail pod. Two acceptance-blocking checks — a FAILURE fails the slice, and
this gate must be re-run on any NeMo version change:

1. Lockfile audit: ``openai`` is ABSENT from the resolved ``uv.lock`` (the locked
   artifact). ``nemoguardrails[server]`` — the extra that pulls ``openai`` — is
   never installed.
2. End-to-end construction: with ``openai.OpenAI`` AND ``openai.AsyncOpenAI``
   monkeypatched to RAISE, a REAL ``LLMRails`` is built through the production
   ``bedrock_engine.build_rails`` path (langchain framework + ``bedrock_converse``
   engine). Construction is where the OpenAI-compatible ``default`` framework
   WOULD build an OpenAI client — so asserting the sentinel is never called, and
   that the inner model is ``ChatBedrockConverse``, proves the Bedrock-only path.
   Construction is offline (builds the boto3 client, makes NO Bedrock call).

``openai`` is not even installed in this pod's environment (a strictly stronger
guarantee than the eval pod, where deepeval pulls it), so the sentinel is
injected into ``sys.modules`` before construction: if any code path reached for
``import openai`` it would hit the raising sentinel and fail loudly.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import types
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
_LOCK_PATH = _SERVICE_ROOT / "uv.lock"
_FIXTURE_CONFIG = str(Path(__file__).resolve().parent / "fixtures" / "nemo_config")


def _boom_openai(*_: object, **__: object):  # pragma: no cover - must never run
    raise AssertionError(
        "An OpenAI client was constructed on the Bedrock guardrail pod. The "
        "no-OpenAI hard gate FAILED: an OpenAI path reappeared. Verify the "
        "`nemoguardrails[server]` extra was not installed and re-run this gate "
        "on the current NeMo version."
    )


def test_openai_absent_from_resolved_lock():
    """Lockfile audit: no `openai` package in the resolved uv.lock (I1 pre-check)."""
    data = tomllib.loads(_LOCK_PATH.read_text())
    names = {pkg.get("name") for pkg in data.get("package", [])}
    assert "openai" not in names, (
        "`openai` resolved into services/guardrail/uv.lock — the "
        "`nemoguardrails[server]` extra (or a transitive dep) pulled it. I1 is "
        "acceptance-blocking: remove it before shipping."
    )
    # openai is not even importable in this pod's environment.
    assert importlib.util.find_spec("openai") is None


def test_no_openai_client_constructed_building_rails(monkeypatch):
    """Building the real LLMRails engine never constructs an OpenAI client."""
    # Inject a sentinel `openai` module whose clients RAISE, so ANY accidental
    # OpenAI construction fails loudly (openai is otherwise absent here).
    sentinel = types.ModuleType("openai")
    sentinel.OpenAI = _boom_openai  # type: ignore[attr-defined]
    sentinel.AsyncOpenAI = _boom_openai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", sentinel)

    from app.bedrock_engine import build_rails

    # Real construction through the production path (offline: no Bedrock call).
    rails = build_rails(_FIXTURE_CONFIG)

    # build_rails asserts the inner model is ChatBedrockConverse; re-confirm here
    # so the gate documents the Bedrock-only engine it proved.
    inner = getattr(rails.llm, "_llm", rails.llm)
    assert type(inner).__name__ == "ChatBedrockConverse"
