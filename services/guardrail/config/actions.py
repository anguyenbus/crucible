"""
Registered NeMo custom action for the pod's FIRST, deterministic output rail.

NeMo auto-loads this ``config/actions.py`` at engine construction (its
``ActionDispatcher.load_actions_from_path``), so ``deterministic_output_scan``
below is a first-class registered action the Colang flow
``config/rails/deterministic_output.co`` invokes — the deterministic secrets
guard is expressed AS a NeMo rail (captured by the config digest), not a step
bolted on outside the rails pipeline.

It is PURE regex (``app.detectors``) — NO LLM / model call — so on a secrets hit
it BLOCKS before, and short-circuits, the paid ``self check output`` /
``self check facts`` LLM rails. The pod's authoritative output path
(``app.nemo_runtime.check_output``) runs the same detector FIRST in-process so
the short-circuit skips the paid ``generate`` entirely and the ``detections``
attribution is recorded on the response; this action makes the rail genuinely
runnable inside ``LLMRails.generate`` (and would block there too).
"""

from __future__ import annotations

from typing import Optional

from nemoguardrails.actions import action

from app.detectors import load_default_detectors


@action(name="deterministic_output_scan", is_system_action=True)
async def deterministic_output_scan(context: Optional[dict] = None) -> bool:
    """
    Return True when the generated answer must be BLOCKED (a secret fired).

    Reads the bot message from the NeMo context and runs the pure-regex detector.
    A non-blocking (advisory) detection returns False — the answer proceeds to
    the LLM rails; only a blocking rule returns True so the Colang flow refuses
    and stops (short-circuit).
    """
    bot_message = (context or {}).get("bot_message") or ""
    return load_default_detectors().scan(bot_message).blocked
