"""
Guard-check routes: ``POST /check/input``, ``POST /check/input/triage``, and
``POST /check/output``.

All reuse the ONE :class:`LLMRails` built in lifespan (``app.state.rails``) — no
per-request rails rebuild — and return a PLAIN-DATA response. The stamped
``model_id`` comes from pod settings (``app.state.settings``), NOT from NeMo
(FINDINGS #2). ``/check/output`` also injects the lifespan-compiled deterministic
detector (``app.state.detectors``) so the pure-regex secrets/PII scan runs FIRST
and can short-circuit the paid LLM rails.

``/check/input/triage`` (Group 4) selects the ``input triage`` flow ONLY (an
independent generate() call), so the SHADOW three-way triage rail never
interferes with the still-enforcing ``/check/input`` self-check.

Fail policy (Q2) is applied inside ``app.nemo_runtime``: a rail-invocation error
is caught THERE and mapped to a fail-SAFE block (input), a fail-OPEN advisory
flag (output/facts), or an ``unavailable`` triage verdict, so a transient Bedrock
error yields a 200 verdict rather than a 5xx. The app-level handlers in
``app/main.py`` remain a backstop for any error OUTSIDE the guarded ``generate``
call (e.g. an unexpected mapping bug), returning a clean JSON body rather than a
leaked stack.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app import nemo_runtime
from app.contract import (
    CheckInputRequest,
    CheckOutputRequest,
    CheckResponse,
    TriageResponse,
)

check_router = APIRouter(tags=["check"])


def _require_rails(request: Request):
    """Return the lifespan-built rails or 503 if the engine is not ready."""
    rails = getattr(request.app.state, "rails", None)
    if rails is None:
        raise HTTPException(status_code=503, detail="guard engine not initialized")
    return rails


@check_router.post("/check/input", response_model=CheckResponse)
def check_input(request: Request, body: CheckInputRequest) -> CheckResponse:
    """Self-check ONE user turn via the enforcing `self check input` rail (pre-filter HIT, I6)."""
    rails = _require_rails(request)
    model_id = request.app.state.settings.model_id
    return nemo_runtime.check_input(rails, body.question, model_id=model_id)


@check_router.post("/check/input/triage", response_model=TriageResponse)
def check_input_triage(request: Request, body: CheckInputRequest) -> TriageResponse:
    """
    Triage ONE user turn as attack / offtopic / ok via the Group 4 triage rail.

    Selects the `input triage` flow ONLY — an independent generate() call — so it
    never interferes with the enforcing `/check/input` self-check. The orchestrator
    calls this on EVERY question (unconditional shadow observation); the RAW turn
    is forwarded (never sanitized before the judge).
    """
    rails = _require_rails(request)
    model_id = request.app.state.settings.model_id
    return nemo_runtime.check_input_triage(rails, body.question, model_id=model_id)


@check_router.post("/check/output", response_model=CheckResponse)
def check_output(request: Request, body: CheckOutputRequest) -> CheckResponse:
    """Self-check a generated answer against its grounding chunks (per-answer).

    The deterministic secrets/PII detector runs FIRST (short-circuiting the paid
    LLM rails on a secret/high-sev-PII block); ``self check facts`` runs only when
    ``body.check_facts`` is set — the independent facts gate (Q6).
    """
    rails = _require_rails(request)
    model_id = request.app.state.settings.model_id
    detectors = getattr(request.app.state, "detectors", None)
    return nemo_runtime.check_output(
        rails,
        body.answer,
        body.chunks,
        model_id=model_id,
        check_facts=body.check_facts,
        detectors=detectors,
    )
