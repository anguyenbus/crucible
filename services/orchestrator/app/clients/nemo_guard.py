"""
NeMo Guardrails pod client: a thin HTTP wrapper over the guardrail pod.

Speaks to the out-of-process ``services/guardrail/`` FastAPI pod
(Bedrock-Haiku-backed NeMo rails). Constructed ONCE in FastAPI lifespan and
injected into the guard stages exactly like
:class:`app.clients.guardrail.GuardClassifier` — the pure stage receives the
instance and never imports ``httpx`` / ``nemoguardrails`` / ``langchain`` itself
(the ``stages-pure`` import-linter contract, invariant I2). ALL HTTP +
NeMo-contract detail lives HERE; the stage sees only a
:class:`typing.Protocol` describing :meth:`check_output` / :meth:`check_input`.

Role separation mirrors :class:`GuardClassifier`: this client speaks HTTP to a
SEPARATE pod whose ``type: main`` model is Haiku — DISTINCT from the Sonnet
generator and from eval's judge. The pod STAMPS its configured Haiku id into the
response ``model_id`` (NeMo's own reported id is unreliable — spike FINDINGS #2),
so the orchestrator never guesses it.

Design decision — this client is THIN and mirrors ``GuardClassifier``: it
performs the HTTP round-trip and RAISES on a transport / non-2xx failure (like
``GuardClassifier`` re-raises a botocore ``ClientError``). The per-rail FAIL
POLICY lives in the PURE stage (``app.orchestrator.guardrails``), exactly as
``check_input`` owns the in-house classifier's fail-SAFE policy: the output/facts
lane fails OPEN (deliver the answer + an advisory ``flag``) when this client
raises, while the input lane fails SAFE/BLOCK on an already pre-filter-flagged
question (Q2/Mode B). The pod ALSO fails open internally; this transport-layer
policy handles the distinct case of the pod being unreachable.

Scope: the NeMo lane started OUTPUT + FACTS only (Q6b), and the nemo-all
``legal-rag-default-1.8.0`` config takes the config flip that was always the plan
— it wires :meth:`check_input` too, so the pod's ``self_check_input`` rail owns
the INPUT verdict and the in-house Haiku confirm-step is retired ON THAT CONFIG.
The in-house ``1.0.0``-``1.4.0`` configs leave ``input_self_check`` off and keep
the in-house classifier, so :meth:`check_input` is never reached on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import httpx

# Deterministic, small guard budget: the pod's rails run at temperature 0 and a
# single self-check round-trip should return quickly. A generous ceiling so a
# genuinely slow pod surfaces as a transport error (→ fail OPEN) rather than
# hanging the request path.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
_CHECK_INPUT_PATH: Final[str] = "/check/input"
_CHECK_OUTPUT_PATH: Final[str] = "/check/output"


@dataclass(frozen=True)
class NemoDetection:
    """
    One deterministic detector hit as PLAIN DATA: labels + COUNTS, NO offsets.

    Maps 1:1 onto the pod's ``Detection`` contract (``services/guardrail/
    app/contract.py``), emitted by the pod's pure-regex secrets/PII output rail:
    ``category`` is the class that fired (``"secrets"`` / ``"pii"``), ``label``
    the specific rule (e.g. ``"email"``, ``"credit_card"``), ``count`` how many
    times it matched. The pod NEVER rewrites the answer, so spans/offsets would
    be dead weight — deliberately absent (verdict-only, scoreable data).
    """

    category: str
    label: str
    count: int


@dataclass(frozen=True)
class NemoVerdict:
    """
    One NeMo pod verdict as PLAIN DATA (no httpx / NeMo types).

    Maps 1:1 onto the pod's ``CheckResponse`` contract (``services/guardrail/
    app/contract.py``) and mirrors the shape of
    :class:`app.clients.guardrail.ClassifierVerdict` plus the advisory ``flag``:

    - ``unsafe`` True ⇒ a rail BLOCKED; the pure stage translates this into the
      honest 200 refusal (``GuardrailTripwire`` → ``REFUSAL_TEXT``). NeMo's own
      refusal string is NEVER surfaced — only ``rationale`` (a terse reason) may
      enter the decision envelope / span.
    - ``flag`` True ⇒ advisory fail-OPEN (deliver the answer WITH an advisory
      ``GuardrailDecision``); never itself a block.
    - ``model_id`` is the Haiku id STAMPED by the pod (``None`` only when this
      client had no pod response to read it from — i.e. a transport failure the
      pure stage turns into a fail-OPEN flag, so no model id is available).
    - ``detections`` carries the pod's deterministic secrets/PII rail attribution
      (labels + counts). It is recorded by the pod EVEN when a block
      short-circuits the paid LLM rails, so attribution survives the skip; it is
      empty on the input lane and on a clean output. This is the SEAM that keeps
      the enrichment plain data: the pure stage and the Phase-2 A/B scoring read
      it from here, never from the pod's HTTP body.
    """

    unsafe: bool
    rationale: str | None
    input_tokens: int | None
    output_tokens: int | None
    model_id: str | None
    flag: bool
    detections: tuple[NemoDetection, ...] = field(default=())


def _parse_detections(raw: Any) -> tuple[NemoDetection, ...]:
    """
    Map the pod's ``detections`` array onto plain :class:`NemoDetection` data.

    Defensive by the same rule as :func:`_parse_verdict`: a missing / malformed
    ``detections`` degrades to EMPTY attribution rather than raising, because a
    verdict (block / flag) must never be lost to an enrichment-parsing error —
    the block itself is the safety-critical half.
    """
    if not isinstance(raw, list):
        return ()
    detections: list[NemoDetection] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        detections.append(
            NemoDetection(
                category=str(entry.get("category", "")),
                label=str(entry.get("label", "")),
                count=int(entry.get("count", 0)),
            )
        )
    return tuple(detections)


def _parse_verdict(payload: dict[str, Any]) -> NemoVerdict:
    """Map a pod JSON body onto the plain :class:`NemoVerdict` (defensive reads)."""
    return NemoVerdict(
        unsafe=bool(payload.get("unsafe", False)),
        rationale=payload.get("rationale"),
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        model_id=payload.get("model_id"),
        flag=bool(payload.get("flag", False)),
        detections=_parse_detections(payload.get("detections")),
    )


class NemoGuardClient:
    """
    Thin HTTP wrapper over the guardrail pod: one round-trip, plain verdict.

    Attributes:
        _base_url: Pod base URL (a LOCATION fact from env-driven Settings).
        _http: ``httpx.Client``-like transport (injectable for tests).

    """

    def __init__(
        self,
        *,
        base_url: str,
        http_client: Any | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Create the client against one pod base URL.

        Args:
            base_url: The guardrail pod base URL (e.g. ``http://guardrail:8000``).
                A LOCATION fact injected from ``Settings.nemo_guard_url``.
            http_client: Optional pre-built ``httpx.Client``-like object (tests
                inject a fake); default builds an ``httpx.Client`` bound to
                ``base_url`` with the guard timeout.
            timeout: Per-call timeout (seconds) for the default client.

        """
        self._base_url = base_url.rstrip("/")
        self._http = (
            http_client
            if http_client is not None
            else httpx.Client(base_url=self._base_url, timeout=timeout)
        )

    def check_input(self, question: str) -> NemoVerdict:
        """
        Self-check ONE user turn via the pod's ``/check/input`` rail.

        Wired ONLY on the nemo-all ``1.8.0`` config (``nemo.input_self_check``),
        and ONLY on an orchestrator regex pre-filter HIT — the pre-filter is the
        FREE cost gate, so benign traffic never reaches this method. The
        ``question`` is forwarded RAW (the pre-filter detects on normalized text
        but never sanitizes the payload before the pod's LLM judge sees it).

        Args:
            question: The RAW user turn to self-check.

        Returns:
            A plain :class:`NemoVerdict` (the pod STAMPS ``model_id``).

        Raises:
            httpx.HTTPError: Transport failure or non-2xx pod response — the pure
                stage translates this into the INPUT lane's fail-SAFE block (the
                caller owns the fail policy).

        """
        response = self._http.post(_CHECK_INPUT_PATH, json={"question": question})
        response.raise_for_status()
        return _parse_verdict(response.json())

    def check_output(self, answer: str, chunks: list[str], *, check_facts: bool) -> NemoVerdict:
        """
        Self-check a generated answer via the pod's ``/check/output`` rail(s).

        Runs the pod's deterministic secrets/PII rail (ordered FIRST, and the
        source of the returned ``detections``) plus ``self check output`` over
        ``answer`` and — when ``check_facts`` is True — ALSO the ``self check
        facts`` grounding rail over ``answer`` vs the retrieved ``chunks``.
        ``check_facts`` is forwarded VERBATIM so the independently-gated facts
        category maps straight onto the pod contract.

        Args:
            answer: The generated answer to self-check.
            chunks: Retrieved chunk texts (grounding evidence for facts); may be
                empty when facts is not engaged.
            check_facts: Whether to run the facts rail (the independent gate).

        Returns:
            A plain :class:`NemoVerdict` (the pod STAMPS ``model_id``).

        Raises:
            httpx.HTTPError: Transport failure or non-2xx pod response — the pure
                stage translates this into the output/facts fail-OPEN advisory
                flag (deliver the answer), never a block.

        """
        response = self._http.post(
            _CHECK_OUTPUT_PATH,
            json={"answer": answer, "chunks": chunks, "check_facts": check_facts},
        )
        response.raise_for_status()
        return _parse_verdict(response.json())
