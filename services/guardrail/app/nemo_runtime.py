"""
Translate a NeMo ``LLMRails.generate`` result into the PLAIN-DATA contract, and
apply the per-rail fail policy (Q2) at the pod boundary.

This is the pod-side mapping the orchestrator's I3 translation keys off. It
honours the Phase-0 FINDINGS exactly:

- A BLOCK surfaces as a ``role == "exception"`` turn in ``res.response`` (rail
  exceptions enabled via ``enable_rails_exceptions``); that IS the ``unsafe:true``
  signal. NeMo's own message text is NEVER forwarded — only the exception
  ``type`` is used as a terse rationale (FINDINGS #5).
- Token accounting comes from ``generate(options={"log": {"llm_calls": True}})``
  → ``res.log.llm_calls[i].prompt_tokens/.completion_tokens`` (FINDINGS #3).
- ``model_id`` is STAMPED by the caller from the configured Haiku id — NeMo's
  reported id is unreliable ("unknown"), so it is never read back (FINDINGS #2).
- Input self-check runs the input rail ONLY (``rails.input`` selected,
  everything else off), so a ``/check/input`` call never pays for a wasted main
  generation (FINDINGS #4).

Deterministic FIRST output rail (Phase 0)
-----------------------------------------
``check_output`` runs the pure-regex secrets/PII detector (``app.detectors``)
FIRST — NO LLM call — before dispatching the paid ``self check output`` /
``self check facts`` rails:

- A secret / high-severity-PII hit BLOCKS and SHORT-CIRCUITS: ``check_output``
  returns ``unsafe=true`` WITHOUT calling ``generate`` at all, so the paid LLM
  rails never fire. The deterministic verdict + attribution (``detections``) are
  ALWAYS recorded on the response, even though the LLM rails were skipped.
- Low-severity PII (email/phone) is detected in the SAME pass but does NOT
  short-circuit: it rides along as ``detections`` while the answer still runs the
  LLM rails (advisory flag policy).

This survives Mode A (pod up, Bedrock rail fails): the deterministic block is
pure regex, so secrets/PII protection holds even when the paid rail throttles.

Independent facts activation (locked decision Q6)
-------------------------------------------------
``config.yml`` declares ``self check output`` and ``self check facts`` under
``rails.output.flows``, but WHICH run is chosen PER REQUEST here via
``GenerationOptions.rails.output`` — a LIST of flow names. When the orchestrator's
per-answer request has facts OFF, ``self check facts`` is simply left out of that
list AND NeMo's built-in ``$check_facts`` context variable is left unset, so the
facts rail makes ZERO LLM calls (two independent gates). When facts is ON, the
flow is added to the list and ``check_facts=True`` is placed in the context. This
is why the highest-value / highest-FP-risk grounding rail is A/B-activatable
independently of the output self-check.

Per-rail fail policy (Q2), applied HERE at the pod boundary
-----------------------------------------------------------
The two failure MODES are distinguished explicitly:

1. A GENUINE rail decision — ``generate`` returns normally with (or without) a
   ``role:"exception"`` turn. An exception turn → ``unsafe:true`` (a real block,
   e.g. a policy-violating answer or an unfaithful one); no exception turn →
   allow. This is the rail's own verdict and is honoured as-is.
2. An INFRASTRUCTURE error — ``generate`` RAISES (Bedrock ``ClientError``,
   throttle exhaustion, transport, etc.). The answer's safety is UNKNOWN, so the
   policy diverges by lane:
   - ``check_input`` fails **SAFE**: return ``unsafe=true`` (BLOCK). The
     orchestrator only calls the input lane on a pre-filter HIT (already
     suspicious), so a fail-safe block can never nuke benign traffic — this
     mirrors the in-house ``ClassifierParseError`` → block.
   - ``check_output`` (output self-check AND facts) fails **OPEN**: return the
     answer with ``unsafe=false`` and an advisory ``flag=true``, NEVER a block —
     a flaky guard must not nuke a valid legal answer. (The deterministic
     detector already ran first, so a secrets/high-PII answer was blocked BEFORE
     this fail-open branch is ever reached.)

Only the ``generate`` call itself is wrapped; a genuine block and a mapping bug
are NOT swallowed. Unit-tested with ``LLMRails`` either MOCKED or driven by an
injected fake LLM (no AWS).
"""

from __future__ import annotations

from typing import Any

from app.contract import CheckResponse, Detection
from app.detectors import Detectors

# Built-in library flow names (config.yml rails.output.flows).
FLOW_SELF_CHECK_OUTPUT: str = "self check output"
FLOW_SELF_CHECK_FACTS: str = "self check facts"

# The generate() options that turn on per-LLM-call token logging (FINDINGS #3).
_LOG_LLM_CALLS: dict[str, Any] = {"log": {"llm_calls": True}}

# Input lane: run the input rail ONLY — no main generation, no output/dialog/
# retrieval rails — so a /check/input call never pays for a wasted answer
# generation (FINDINGS #4).
_INPUT_ONLY_RAILS: dict[str, Any] = {
    "input": True,
    "output": False,
    "dialog": False,
    "retrieval": False,
}

# A minimal user turn establishing the conversation turn that carries the
# provided answer as its bot message; without a preceding user turn NeMo runs no
# output rail over a supplied assistant message (verified offline). The content
# is a neutral placeholder — the output/facts prompts key off ``bot_response`` /
# ``evidence``, not this turn.
_OUTPUT_TURN_PLACEHOLDER: str = "(evaluate the assistant answer below)"


def _extract_block(res: Any) -> tuple[bool, str | None]:
    """
    Detect a rail block from the response turns.

    Returns ``(unsafe, rationale)``. A ``role == "exception"`` turn means the
    rail blocked; the rationale is the exception ``type`` (e.g.
    ``OutputRailException``) — a terse, safe label, never NeMo's own prose.
    """
    response = getattr(res, "response", None) or []
    for turn in response:
        if isinstance(turn, dict) and turn.get("role") == "exception":
            content = turn.get("content")
            rationale = None
            if isinstance(content, dict):
                rationale = content.get("type")
            return True, rationale or "rail exception"
    return False, None


def _extract_tokens(res: Any) -> tuple[int | None, int | None]:
    """
    Sum prompt/completion tokens across the logged Bedrock calls (FINDINGS #3).

    Returns ``(input_tokens, output_tokens)``; ``None`` when nothing was logged
    (a mock or a call made without the token-logging option).
    """
    log = getattr(res, "log", None)
    calls = getattr(log, "llm_calls", None) if log is not None else None
    if not calls:
        return None, None
    prompt = 0
    completion = 0
    seen = False
    for call in calls:
        pt = getattr(call, "prompt_tokens", None)
        ct = getattr(call, "completion_tokens", None)
        if pt is not None:
            prompt += pt
            seen = True
        if ct is not None:
            completion += ct
            seen = True
    if not seen:
        return None, None
    return prompt, completion


def _to_response(
    res: Any, *, model_id: str, detections: tuple[Detection, ...] = ()
) -> CheckResponse:
    """Map a generate() result to the plain-data contract, stamping ``model_id``."""
    unsafe, rationale = _extract_block(res)
    input_tokens, output_tokens = _extract_tokens(res)
    return CheckResponse(
        unsafe=unsafe,
        rationale=rationale,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_id=model_id,
        flag=False,
        detections=list(detections),
    )


def _fail_safe_block(model_id: str, exc: Exception) -> CheckResponse:
    """Input-lane infrastructure error → fail SAFE (BLOCK), per Q2."""
    return CheckResponse(
        unsafe=True,
        rationale=f"input guard unavailable ({type(exc).__name__}); failing safe",
        input_tokens=None,
        output_tokens=None,
        model_id=model_id,
        flag=False,
    )


def _fail_open_flag(
    model_id: str, exc: Exception, *, detections: tuple[Detection, ...] = ()
) -> CheckResponse:
    """Output/facts infrastructure error → fail OPEN (deliver + advisory flag), per Q2."""
    return CheckResponse(
        unsafe=False,
        rationale=f"output guard unavailable ({type(exc).__name__}); failing open",
        input_tokens=None,
        output_tokens=None,
        model_id=model_id,
        flag=True,
        detections=list(detections),
    )


def _deterministic_block(
    model_id: str, *, rationale: str | None, detections: tuple[Detection, ...]
) -> CheckResponse:
    """
    A pure-regex secrets/high-PII BLOCK — the short-circuit verdict.

    NO model call was made (regex-only), so token telemetry is ``None``. The
    ``detections`` attribution is ALWAYS recorded, even though the paid LLM rails
    were skipped.
    """
    return CheckResponse(
        unsafe=True,
        rationale=rationale,
        input_tokens=None,
        output_tokens=None,
        model_id=model_id,
        flag=False,
        detections=list(detections),
    )


def check_input(rails: Any, question: str, *, model_id: str) -> CheckResponse:
    """
    Run the input self-check over ONE user turn and map to the contract.

    The orchestrator calls this ONLY on a pre-filter HIT (I6), so the paid guard
    call never fires on benign traffic. The input rail runs ALONE (no main
    generation, FINDINGS #4). An infrastructure error fails SAFE → BLOCK (Q2).
    """
    try:
        res = rails.generate(
            messages=[{"role": "user", "content": question}],
            options={**_LOG_LLM_CALLS, "rails": _INPUT_ONLY_RAILS},
        )
    except Exception as exc:  # noqa: BLE001 — deliberate: any rail-call failure
        return _fail_safe_block(model_id, exc)
    return _to_response(res, model_id=model_id)


def check_output(
    rails: Any,
    answer: str,
    chunks: list[str],
    *,
    model_id: str,
    check_facts: bool = False,
    detectors: Detectors | None = None,
) -> CheckResponse:
    """
    Deterministic FIRST rail, then the output self-check (and optional facts rail).

    When ``detectors`` is provided (the pod always injects the lifespan-built
    detector), the pure-regex secrets/PII scan runs FIRST:

    - A secret / high-severity-PII hit returns a BLOCK immediately WITHOUT calling
      ``generate`` — the paid ``self check output`` / ``self check facts`` LLM
      rails are SHORT-CIRCUITED. The deterministic verdict + ``detections`` are
      recorded on the response.
    - Otherwise (clean, or low-severity PII only) the scan's ``detections`` (which
      may carry advisory email/phone counts) ride along while the LLM rails run.

    ``self check output`` always runs (LLM path). ``self check facts`` runs ONLY
    when ``check_facts`` is True — added to the per-request ``rails.output`` flow
    list AND ``check_facts=True`` placed in the NeMo context. With ``check_facts``
    False the facts rail makes ZERO LLM calls (independent gating, Q6). An
    infrastructure error fails OPEN → deliver the answer with an advisory ``flag``
    (never a block, Q2), carrying any deterministic ``detections``.
    """
    detections: tuple[Detection, ...] = ()
    if detectors is not None:
        scan = detectors.scan(answer)
        detections = scan.detections
        if scan.blocked:
            # SHORT-CIRCUIT: skip the paid LLM rails entirely; record the verdict.
            return _deterministic_block(
                model_id, rationale=scan.rationale, detections=detections
            )

    context: dict[str, Any] = {}
    if chunks:
        context["relevant_chunks"] = chunks
    output_flows = [FLOW_SELF_CHECK_OUTPUT]
    if check_facts:
        context["check_facts"] = True
        output_flows.append(FLOW_SELF_CHECK_FACTS)

    messages = [
        {"role": "context", "content": context},
        {"role": "user", "content": _OUTPUT_TURN_PLACEHOLDER},
        {"role": "assistant", "content": answer},
    ]
    try:
        res = rails.generate(
            messages=messages,
            options={
                **_LOG_LLM_CALLS,
                "rails": {
                    "input": False,
                    "output": output_flows,
                    "dialog": False,
                    "retrieval": False,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 — deliberate: any rail-call failure
        return _fail_open_flag(model_id, exc, detections=detections)
    return _to_response(res, model_id=model_id, detections=detections)
