"""
NeMo LLM-framework shim (the ``bedrock_engine.py`` the spec anticipated).

Phase-0 CRITICAL finding (FINDINGS.md): ``nemoguardrails==0.23.0`` split its LLM
path into two frameworks. The GLOBAL default (``default``) is OpenAI-compatible
and RAISES ``ValueError`` for ``bedrock`` — only the ``langchain`` framework
reaches Bedrock. There is NO per-model / config.yml field for this; it is chosen
globally by ``get_default_framework()`` reading
``NEMOGUARDRAILS_LLM_FRAMEWORK`` at import time.

Defence in depth (both are applied):
- the Dockerfile sets ``NEMOGUARDRAILS_LLM_FRAMEWORK=langchain`` (read at import), AND
- :func:`force_langchain_framework` calls ``set_default_framework("langchain")``
  in the FastAPI lifespan BEFORE :class:`LLMRails` is constructed — order-independent,
  so a missing env can never silently drop the pod onto the OpenAI path.

Importing this module performs NO I/O and constructs NO client.
"""

from __future__ import annotations

import os
from typing import Any

# The langchain-framework path builds ``ChatBedrockConverse`` from the
# ``bedrock_converse`` engine token (FINDINGS unknown (a), PROVEN). Anything else
# means the framework shim did not take.
_EXPECTED_INNER_MODEL: str = "ChatBedrockConverse"

# NeMo's anonymous usage-telemetry opt-out vars. ``LLMRails.__init__`` calls
# ``nemoguardrails.telemetry.report_usage``, which POSTs a usage event to an
# NVIDIA endpoint and starts a heartbeat daemon thread, unless one of these is
# set. NeMo honours EITHER (``telemetry.py`` ``_is_usage_stats_enabled``); both
# are set so the opt-out survives someone "cleaning up" the vendor-specific one.
_TELEMETRY_OPT_OUT: dict[str, str] = {
    "NEMO_GUARDRAILS_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
}


def disable_usage_telemetry() -> None:
    """
    Disable NeMo's anonymous usage telemetry (NO unsolicited egress from the pod).

    This pod guards legal / accounting traffic, so the beacon
    ``LLMRails.__init__`` would otherwise fire on every engine construction is
    not acceptable regardless of how anonymous its payload is.

    ``_is_usage_stats_enabled`` reads the environment INSIDE ``report_usage`` (at
    construction time, not at import), so setting the vars any time before
    :class:`LLMRails` is built is sufficient. Idempotent; deliberately does NOT
    overwrite an existing value, so an operator can never be silently
    contradicted — only the absent case is filled in.
    """
    for name, value in _TELEMETRY_OPT_OUT.items():
        os.environ.setdefault(name, value)


def force_langchain_framework() -> None:
    """
    Force NeMo's global LLM framework to ``langchain`` (Bedrock's only path).

    Idempotent and order-independent: mutates the live global regardless of
    import order. Call it in lifespan BEFORE constructing :class:`LLMRails`.
    """
    from nemoguardrails.llm.frameworks import set_default_framework

    set_default_framework("langchain")


def build_rails(config_dir: str) -> Any:
    """
    Build the ONE reusable :class:`LLMRails` from ``config_dir`` (Bedrock-only).

    Forces the langchain framework first, then constructs ``LLMRails`` from
    ``RailsConfig.from_path(config_dir)``. Construction is offline — it builds
    the ``ChatBedrockConverse`` client on the ambient AWS credential chain but
    makes NO Bedrock call (paid calls happen only on ``generate``). Returns a
    duck-typed instance so nothing here leaks NeMo types to callers by type.

    Raises:
        RuntimeError: the constructed inner model is not ``ChatBedrockConverse``
            — the framework shim failed and the pod would silently use a wrong
            (or OpenAI) backend. Fail loud rather than serve on the wrong engine.
    """
    # Kill the usage beacon BEFORE LLMRails is constructed — that constructor is
    # what fires it. Defence in depth with the Dockerfile ENV.
    disable_usage_telemetry()
    force_langchain_framework()
    # Imported lazily so importing this module (e.g. from the settings-only unit
    # tests) does not require the full NeMo import graph.
    from nemoguardrails import LLMRails, RailsConfig

    config = RailsConfig.from_path(config_dir)
    rails = LLMRails(config)

    inner = getattr(rails.llm, "_llm", rails.llm)
    if type(inner).__name__ != _EXPECTED_INNER_MODEL:
        raise RuntimeError(
            "NeMo LLM-framework shim did not take: expected inner model "
            f"{_EXPECTED_INNER_MODEL!r} (langchain framework + bedrock_converse "
            f"engine), got {type(inner).__name__!r}. Refusing to serve on the "
            "wrong backend."
        )
    return rails
