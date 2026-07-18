"""
Guard-check routes: ``POST /check/input`` and ``POST /check/output``.

Both reuse the ONE :class:`LLMRails` built in lifespan (``app.state.rails``) —
no per-request rails rebuild — and return the PLAIN-DATA :class:`CheckResponse`.
The stamped ``model_id`` comes from pod settings (``app.state.settings``), NOT
from NeMo (FINDINGS #2). ``/check/output`` also injects the lifespan-compiled
deterministic detector (``app.state.detectors``) so the pure-regex secrets/PII
scan runs FIRST and can short-circuit the paid LLM rails.

Fail policy (Q2) is applied inside ``app.nemo_runtime``: a rail-invocation error
is caught THERE and mapped to a fail-SAFE block (input) or fail-OPEN advisory
flag (output/facts), so a transient Bedrock error yields a 200 verdict rather
than a 5xx. The app-level handlers in ``app/main.py`` remain a backstop for any
error OUTSIDE the guarded ``generate`` call (e.g. an unexpected mapping bug),
returning a clean JSON body rather than a leaked stack.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app import nemo_runtime
from app.contract import CheckInputRequest, CheckOutputRequest, CheckResponse

check_router = APIRouter(tags=["check"])


def _require_rails(request: Request):
    """Return the lifespan-built rails or 503 if the engine is not ready."""
    rails = getattr(request.app.state, "rails", None)
    if rails is None:
        raise HTTPException(status_code=503, detail="guard engine not initialized")
    return rails


@check_router.post("/check/input", response_model=CheckResponse)
def check_input(request: Request, body: CheckInputRequest) -> CheckResponse:
    """Self-check ONE user turn (called only on a pre-filter HIT, I6)."""
    rails = _require_rails(request)
    model_id = request.app.state.settings.model_id
    return nemo_runtime.check_input(rails, body.question, model_id=model_id)


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
