"""
Registered NeMo custom actions for the pod's deterministic output rail and the
Group 4 input triage rail.

NeMo auto-loads this ``config/actions.py`` at engine construction (its
``ActionDispatcher.load_actions_from_path``), so the actions below are
first-class registered actions the Colang flows in ``config/rails/`` invoke:

- ``deterministic_output_scan`` — the pure-regex secrets guard, expressed AS a
  NeMo rail (``config/rails/deterministic_output.co``), captured by the config
  digest, that BLOCKS before the paid ``self check output`` / ``self check facts``
  LLM rails.
- ``input_triage_check`` — the Group 4 Haiku triage action
  (``config/rails/input_triage.co``): ONE Bedrock Haiku call returning ONE of
  ``attack`` / ``offtopic`` / ``ok``, label-FIRST, ``max_tokens: 4``. The flow
  branches the two blocking labels to TWO distinct exception types so the
  orchestrator can tell an attack-block from a topic-redirect.
"""

from __future__ import annotations

from typing import Optional

from nemoguardrails import RailsConfig
from nemoguardrails.actions import action
from nemoguardrails.actions.llm.utils import llm_call
from nemoguardrails.context import llm_call_info_var
from nemoguardrails.llm.taskmanager import LLMTaskManager
from nemoguardrails.logging.explain import LLMCallInfo
from nemoguardrails.types import LLMModel

from app.detectors import load_default_detectors

# The custom triage task name (matches the `task: input_triage` entry in
# prompts.yml; `render_task_prompt`/`get_max_tokens` accept a plain string task).
_TRIAGE_TASK: str = "input_triage"
# A fail-safe fallback cap ONLY used if the prompt somehow declares none — the
# prompt pins `max_tokens: 4` as the load-bearing security property (see
# prompts.yml). Kept small so even the fallback cannot decode an amplified loop.
_TRIAGE_MAX_TOKENS_FALLBACK: int = 4


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


@action(name="input_triage_check", is_system_action=True)
async def input_triage_check(
    llm_task_manager: LLMTaskManager,
    context: Optional[dict] = None,
    llm: Optional[LLMModel] = None,
    config: Optional[RailsConfig] = None,
    **kwargs,
) -> str:
    """
    Classify ONE user turn as ``attack`` / ``offtopic`` / ``ok`` (label-first).

    Mirrors the built-in ``self_check_input`` action's LLM-call shape but renders
    the custom ``input_triage`` task prompt and returns a THREE-way label string
    that ``config/rails/input_triage.co`` branches on. The verdict is single-token
    and label-FIRST; ``max_tokens`` comes from the prompt (pinned to 4 — a
    reasoning-DoS security property, see prompts.yml). NO stop sequence is passed
    (Bedrock Converse rejects a blank stop).

    Parse fails toward ``ok``: an unrecognised completion returns ``ok`` rather
    than refusing a legitimate case question on a parser hiccup — topicality is a
    product-quality feature, and the deterministic ATTACK regex floor sits behind
    this rail in the orchestrator. The RAW user turn is classified (the pod never
    sanitizes the payload before the judge sees it).
    """
    user_input = (context or {}).get("user_message")
    if not user_input:
        return "ok"

    prompt = llm_task_manager.render_task_prompt(
        task=_TRIAGE_TASK,
        context={"user_input": user_input},
    )
    max_tokens = llm_task_manager.get_max_tokens(task=_TRIAGE_TASK) or _TRIAGE_MAX_TOKENS_FALLBACK

    # Record the LLM call under the triage task name so token telemetry is
    # attributable (parallels the built-in self-check actions).
    llm_call_info_var.set(LLMCallInfo(task=_TRIAGE_TASK))

    llm_response = await llm_call(
        llm,
        prompt,
        llm_params={
            "temperature": config.lowest_temperature if config is not None else 0.0,
            "max_tokens": max_tokens,
        },
    )
    raw = (llm_response.content or "").strip().lower()

    # Label-FIRST parse: the design depends on the model emitting the label as the
    # first token (the completion is truncated at 4 tokens). Match on the leading
    # word so a trailing fragment ('OK.', 'ATTACK -') still parses.
    if raw.startswith("attack"):
        return "attack"
    if raw.startswith("offtopic") or raw.startswith("off-topic") or raw.startswith("off topic"):
        return "offtopic"
    # Fail toward OK on anything else (incl. an empty/garbled completion).
    return "ok"
