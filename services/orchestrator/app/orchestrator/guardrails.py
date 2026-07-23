"""
Input/output guardrail handling stage (system-prompt-leakage input guard +
deterministic output PII/secrets guard).

Layer 1 of the guard: a config-gated, deterministic prompt-leak/injection
pre-filter → an INJECTED Bedrock Haiku classifier (called ONLY on a pre-filter
hit) → a structured allow/block decision. When it blocks it raises
:class:`GuardrailTripwire` carrying a :class:`GuardrailDecision`; the router
turns that into a 200 canned refusal (NOT an error — a tripwire is a
successful, honest refusal and must never reach the 5xx handlers).

Purity: this stage is a PURE function of its typed inputs and the injected
classifier — it imports NO infra library (boto3/opensearch/OTel). The injected
classifier is described with a :class:`typing.Protocol` (structural typing) so
NO ``app.clients`` import is needed (the ``stages-pure`` import-linter contract
and ``check_stages_grep_gate.sh`` stay green; the client is injected exactly
like ``bedrock``/``search`` are into the other stages).

``check_output`` is a PURE, deterministic (regex-only, NO model call) PII +
secrets scan of the generated answer (roadmap Phase 3 item 10): a secrets hit
raises :class:`GuardrailTripwire` (200 canned refusal); redactable PII spans are
masked in place with a fixed token (one ``transform`` decision); advisory PII
(email/phone) is flagged with the answer unchanged. It is config-gated by
``pins.output_categories`` — an empty tuple (the released ``1.0.0``–``1.3.0``
configs) makes it a typed identity, so those lanes stay byte-for-byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol

from app.schemas.envelope import GuardrailDecision
from app.schemas.pipeline_config import GuardrailsPin

# The single canned refusal the Chainlit UI renders verbatim on a block.
REFUSAL_TEXT: Final[str] = "I'm sorry, but I can't help with that request."

_INPUT_STAGE: Final[str] = "input"
_OUTPUT_STAGE: Final[str] = "output"

# Pre-filter detector sub-classes — the FREE cost gate in front of the pod's
# `self_check_input`. `prompt_leak` is the shipped `_PREFILTER_PATTERNS` set;
# `jailbreak` and `unicode_evasion` are the second-slice additions. These decide
# NOTHING: a hit only means the paid pod call is worth making.
_PROMPT_LEAK_INPUT_CATEGORY: Final[str] = "prompt_leak"
_JAILBREAK_INPUT_CATEGORY: Final[str] = "jailbreak"
_UNICODE_EVASION_INPUT_CATEGORY: Final[str] = "unicode_evasion"

# NeMo out-of-process OUTPUT/facts lane identities (self check output +
# independently-gated self check facts). A NeMo block reuses the SAME honest
# 200-refusal path as the regex secrets block (I3); an advisory flag delivers
# the answer with a non-block decision (Q2 output/facts fail-OPEN).
_NEMO_CATEGORY: Final[str] = "nemo"
_NEMO_OUTPUT_BLOCK_RULE_ID: Final[str] = "nemo-output-block-v1"
_NEMO_OUTPUT_FLAG_RULE_ID: Final[str] = "nemo-output-flag-v1"
# The Mode-B FAIL-OPEN window carries its OWN rule id, distinct from the routine
# advisory above. Both deliver the answer with a `flag`, but they mean opposite
# things: an advisory is the pod TELLING us something, whereas a fail-open means
# the pod never answered and the response is UNGUARDED. Alarming on the rationale
# prose would be fragile, so the machine-readable rule id is what makes the window
# LOUD — it rides the `guardrail.rule_id` span attribute into Phoenix, so a
# sustained outage is a countable signal and a silent fail-open is impossible.
_NEMO_OUTPUT_FAIL_OPEN_RULE_ID: Final[str] = "nemo-output-fail-open-v1"
_NEMO_DEFAULT_BLOCK_RATIONALE: Final[str] = (
    "NeMo output/facts rail blocked the answer"
)
_NEMO_ADVISORY_RATIONALE: Final[str] = "NeMo output/facts advisory"
_NEMO_FAIL_OPEN_RATIONALE: Final[str] = (
    "NeMo guardrail pod unavailable on the output/facts path — failing open"
)
# NeMo out-of-process INPUT self-check lane identities (the nemo-all `1.8.0`
# config's `self_check_input`). A block reuses the SAME honest 200-refusal path
# as the in-house prompt-leak block (I3); a pre-filter-flagged input whose pod
# call FAILS fails SAFE/BLOCK (Mode B), mirroring the in-house classifier's
# fail-safe on a flagged input.
_NEMO_INPUT_BLOCK_RULE_ID: Final[str] = "nemo-input-block-v1"
_NEMO_INPUT_DEFAULT_BLOCK_RATIONALE: Final[str] = "NeMo input rail blocked the question"
_NEMO_INPUT_FAIL_SAFE_RATIONALE: Final[str] = (
    "NeMo guardrail pod unavailable on a pre-filter-flagged input — failing safe"
)
# The FULL pre-filter category set the NeMo input lane runs as its FREE cost
# gate, DECOUPLED from the in-house `input_categories` decision gate (empty on
# `1.8.0`): prompt_leak + jailbreak + unicode_evasion (invisible-char strip +
# BIDI). It DETECTS on normalized text but the RAW question is forwarded to the
# pod — the adversarial payload is never sanitized before `self_check_input`.
_NEMO_INPUT_PREFILTER_CATEGORIES: Final[tuple[str, ...]] = (
    _PROMPT_LEAK_INPUT_CATEGORY,
    _JAILBREAK_INPUT_CATEGORY,
    _UNICODE_EVASION_INPUT_CATEGORY,
)


@dataclass(frozen=True)
class GuardInputResult:
    """
    Outcome of the input guard: the (unchanged) question + classifier-call telemetry.

    ``model_id``/``input_tokens``/``output_tokens`` are populated ONLY when the
    classifier actually ran (a pre-filter hit classified SAFE); a gate-off or
    pre-filter-miss result carries just the question. The router attaches the
    telemetry to the ``guardrail_input`` LLM span so the Haiku guard call is
    visible in Phoenix (model id, token counts, latency) on a SAFE allow — not
    only on a block.
    """

    question: str
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class GuardOutputResult:
    """
    Outcome of the output guard: the possibly-redacted answer + its decisions.

    Parallel to :class:`GuardInputResult`. ``answer_text`` is the generated
    answer with every redactable PII span masked in place (or the original text
    unchanged when nothing matched / the guard is off). ``decisions`` accumulates
    the non-block outcomes — at most one ``transform`` (PII redaction, with a
    per-rule count) and one ``flag`` (advisory email/phone, with a count). A
    secrets hit never produces a :class:`GuardOutputResult`: it raises
    :class:`GuardrailTripwire` instead (the whole answer is suppressed).
    """

    answer_text: str
    decisions: tuple[GuardrailDecision, ...] = ()
    # NeMo output-lane guard-call telemetry, populated ONLY by
    # ``check_output_nemo`` (the regex ``check_output`` never sets them, so its
    # result stays byte-identical — these defaulted fields are additive). The
    # router attaches them to the ``guardrail_output`` span so the NeMo guard
    # call's cost is visible in Phoenix on an allow/advisory outcome too.
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class NemoOutputVerdict(Protocol):
    """
    Structural shape of the NeMo pod's plain-data OUTPUT verdict.

    Matches ``app.clients.nemo_guard.NemoVerdict`` WITHOUT importing it, so
    the stage stays free of any httpx/nemoguardrails-carrying module
    (``stages-pure``, I2). ``unsafe`` True ⇒ a rail blocked; ``flag`` True ⇒
    an advisory fail-OPEN deliver-with-flag; ``model_id`` is the pod-stamped
    Haiku id (``None`` only when there was no pod response).
    """

    unsafe: bool
    rationale: str | None
    input_tokens: int | None
    output_tokens: int | None
    model_id: str | None
    flag: bool


class NemoGuardClient(Protocol):
    """
    Structural type of the injected out-of-process NeMo Guardrails pod client.

    Matches ``app.clients.nemo_guard.NemoGuardClient`` WITHOUT importing it —
    the stage stays free of any httpx / nemoguardrails / langchain import
    (``stages-pure`` import-linter contract + ``check_stages_grep_gate.sh``,
    invariant I2). The client is injected by the router exactly like
    ``bedrock``/``search``/``classifier``; ALL HTTP + NeMo detail lives in
    ``app.clients.nemo_guard``. Q6b (locked): the NeMo lane is OUTPUT + FACTS
    ONLY this slice, so the pure stage needs ONLY ``check_output``.
    """

    def check_output(
        self, answer: str, chunks: list[str], *, check_facts: bool
    ) -> NemoOutputVerdict:
        """Self-check a generated answer (+ optional facts grounding) via the pod."""
        ...

    def check_input(self, question: str) -> NemoOutputVerdict:
        """
        Self-check ONE user turn via the pod's ``self_check_input`` rail.

        Wired ONLY on the nemo-all ``1.8.0`` config (``input_self_check``); the
        in-house ``1.0.0``-``1.4.0`` configs keep the in-house Haiku
        confirm-step and never reach this method (Q6b).
        """
        ...


# Deterministic pre-filter: a curated pattern set for the prompt-leak /
# prompt-injection extraction class. A MISS returns the question unchanged with
# NO paid classifier call (cost ≈ 0 on normal traffic); a HIT is a *candidate*
# that the Haiku classifier then confirms or clears.
_PREFILTER_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"system prompt",
        r"your (instructions|prompt|rules|guidelines)",
        r"ignore (all |the )?(previous|prior|above|preceding)",
        r"disregard .*(instruction|prompt|rule)",
        r"repeat .*(above|verbatim|prompt|instruction)",
        r"reveal .*(prompt|instruction|rule)",
        r"(print|show|output|display) .*(prompt|instruction|the text above)",
        r"(what|repeat) .*(text|words) above",
        r"initial (instructions|prompt)",
    )
)


# ===========================================================================
# Borrowed deterministic detector tables (output guard + input hardening).
#
# These are module-level compiled-regex / codepoint constants styled exactly
# like ``_PREFILTER_PATTERNS`` above: PURE regex/stdlib, NO infra import, so the
# ``stages-pure`` import-linter contract and ``check_stages_grep_gate.sh`` stay
# GREEN with no amendment. Only the pattern TABLES (facts) are lifted from the
# FutureAGI ``agent-learning-kit`` scanners -- NOT their
# ``BaseScanner``/``ScanResult``/registry class hierarchy, which is
# reimplemented against OUR ``GuardrailDecision`` / ``GuardOutputResult`` types.
# A source-attribution comment sits on each table.
#
# Entry shape mirrors ``_PREFILTER_PATTERNS`` but each row also carries a rule
# label (and the source confidence, retained for provenance) so a decision's
# rationale can name which class fired. Every table is consumed only when its
# category is present in the config's ``input_categories`` / ``output_categories``
# tuple -- these are inert facts, not behavior, on their own.
# ===========================================================================

# --- Pattern set 1: secrets / credentials (OUTPUT guard, BLOCK class) --------
# Source: references/agent-learning-kit/python/fi/evals/guardrails/scanners/secrets.py
# (API_KEY_PATTERNS, PRIVATE_KEY_PATTERNS, JWT_PATTERNS, CONNECTION_STRING_PATTERNS).
# HIGH-confidence, vendor-prefixed subset ONLY -- the FP-prone entropy /
# password-assignment / bare bank_account patterns are deliberately SKIPPED
# (a legal-RAG answer can trip them). Compiled WITHOUT a global IGNORECASE flag
# so case-sensitive prefixes (``AKIA``, ``sk-ant-``) stay exact; the one
# case-insensitive rule (the DB URL) carries its own inline ``(?i)``.
# (regex, rule_label, source_confidence)
_INVISIBLE: Final[frozenset[int]] = frozenset(
    {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x180E, 0x2061, 0x2062, 0x2063, 0x2064}
)
_BIDI: Final[frozenset[int]] = frozenset(
    {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}
)


def _strip_invisible(text: str) -> str:
    """Drop every zero-width / invisible codepoint so unicode smuggling can't hide a keyword."""
    return "".join(ch for ch in text if ord(ch) not in _INVISIBLE)


def _has_bidi_override(text: str) -> bool:
    """Return True when any BIDI override codepoint (e.g. U+202E) is present."""
    return any(ord(ch) in _BIDI for ch in text)


# --- Pattern set 4: jailbreak (INPUT hardening candidate set) -----------------
# Source: references/agent-learning-kit/python/fi/evals/guardrails/scanners/jailbreak.py
# (JAILBREAK_PATTERNS). HIGH-confidence (source conf >= 0.85) subset: DAN,
# roleplay/instruction override, dev/god mode, token smuggling. Kept as its OWN
# table, SEPARATE from the unchanged prompt-leak ``_PREFILTER_PATTERNS`` so a
# ``[prompt_leak]``-only config's pre-filter stays byte-for-byte. This is a
# COST-GATE candidate set that feeds the SAME Haiku classifier -- it never
# blocks by itself.
# (regex, rule_label, source_confidence)
_JAILBREAK_PATTERNS: Final[tuple[tuple[re.Pattern[str], str, float], ...]] = tuple(
    (re.compile(pattern), label, confidence)
    for pattern, label, confidence in (
        (r"(?i)\bDAN\b.*?(?:mode|jailbreak|unlock)", "dan_mode", 0.95),
        (r"(?i)do\s+anything\s+now", "do_anything_now", 0.90),
        (
            r"(?i)ignore\s+(?:all\s+)?(?:your\s+)?(?:previous|prior|ethical|safety)"
            r"\s+(?:instructions|guidelines|training|rules)",
            "instruction_override",
            0.90,
        ),
        (
            r"(?i)(?:disregard|ignore|bypass)\s+(?:all\s+)?(?:safety|content|ethical)"
            r"\s+(?:filters?|guidelines?|policies?)",
            "safety_bypass",
            0.90,
        ),
        (r"(?i)(?:developer|dev|sudo|admin|god|root)\s+mode", "privilege_mode", 0.85),
        (r"(?i)\[(?:INST|SYS|SYSTEM|USER|ASSISTANT)\]", "token_smuggling", 0.85),
        (
            r"(?i)<\|(?:im_start|im_end|system|user|assistant)\|>",
            "special_token_injection",
            0.85,
        ),
        (
            r"(?i)(?:new|override|replace)\s+(?:system\s+)?(?:prompt|instructions?|rules?)",
            "prompt_override",
            0.85,
        ),
    )
)


class GuardrailTripwire(Exception):
    """
    A successful, honest refusal — NOT an error.

    Raised by :func:`check_input` (input block) and :func:`check_output` (output
    secrets block); carries the :class:`GuardrailDecision` the router attaches to
    the refusal envelope's ``guardrail_decisions[]`` and records as a guardrail
    span. It must NEVER be routed through the app-level 5xx exception handlers
    (the router catches it specifically and returns a 200 canned refusal).
    """

    def __init__(
        self,
        decision: GuardrailDecision,
        *,
        model_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """
        Carry the block decision + guard-call telemetry to the router.

        The router records both on the ``guardrail_input`` LLM span; ``model_id``
        and tokens are ``None`` on a fail-safe block (no verdict) and on an
        output secrets block (regex-only, no model call).
        """
        super().__init__(decision.rationale or REFUSAL_TEXT)
        self.decision = decision
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class GuardMisconfiguredError(RuntimeError):
    """
    The guard is ENABLED but its classifier client / model id was never injected.

    A deploy/wiring error, NOT a per-request classifier failure — raised LOUDLY
    (it propagates to a 500, distinct from :class:`GuardrailTripwire`) instead of
    being masked as a fail-safe refusal, so a whole class of queries is never
    silently blocked by a misconfiguration. In the real build path the classifier
    is ALWAYS constructed (``build_app_clients`` makes no paid call), so this
    signals a genuine wiring bug rather than any expected runtime condition.
    """


def _nemo_output_block_decision(rationale: str) -> GuardrailDecision:
    """Build the NeMo output-lane ``block`` decision (honest 200 refusal path, I3)."""
    return GuardrailDecision(
        stage=_OUTPUT_STAGE,
        decision="block",
        category=_NEMO_CATEGORY,
        rule_id=_NEMO_OUTPUT_BLOCK_RULE_ID,
        rationale=rationale,
    )


def _nemo_output_fail_open_decision(rationale: str) -> GuardrailDecision:
    """
    Build the LOUD Mode-B fail-open decision (pod unreachable → answer UNGUARDED).

    Shaped like the advisory flag (non-blocking: a flaky pod must never nuke a
    valid legal answer) but carrying :data:`_NEMO_OUTPUT_FAIL_OPEN_RULE_ID` so the
    fail-open window is distinguishable from a genuine pod advisory by rule id
    alone — the alarm/telemetry signal, not prose.
    """
    return GuardrailDecision(
        stage=_OUTPUT_STAGE,
        decision="flag",
        category=_NEMO_CATEGORY,
        rule_id=_NEMO_OUTPUT_FAIL_OPEN_RULE_ID,
        rationale=rationale,
    )


def _nemo_output_flag_decision(rationale: str) -> GuardrailDecision:
    """Build the NeMo output-lane advisory ``flag`` decision (answer delivered, Q2)."""
    return GuardrailDecision(
        stage=_OUTPUT_STAGE,
        decision="flag",
        category=_NEMO_CATEGORY,
        rule_id=_NEMO_OUTPUT_FLAG_RULE_ID,
        rationale=rationale,
    )


def _prefilter_hit(question: str, categories: tuple[str, ...]) -> bool:
    """
    Return True when any ACTIVE pre-filter detector class matches ``question``.

    Each detector sub-class is individually gated by ``categories`` (the
    config's ``input_categories``): ``prompt_leak`` runs the unchanged
    ``_PREFILTER_PATTERNS``; ``jailbreak`` runs the borrowed jailbreak table;
    ``unicode_evasion`` strips invisible codepoints BEFORE keyword matching and
    treats a BIDI override as a standalone hit. A ``[prompt_leak]``-only config
    applies no normalization, so its pre-filter is byte-for-byte the shipped set.
    """
    active = frozenset(categories)

    # unicode_evasion: a BIDI override is a standalone hit; otherwise normalize
    # away invisible codepoints so a zero-width-smuggled keyword can't bypass.
    if _UNICODE_EVASION_INPUT_CATEGORY in active:
        if _has_bidi_override(question):
            return True
        scan_text = _strip_invisible(question)
    else:
        scan_text = question

    if _PROMPT_LEAK_INPUT_CATEGORY in active and any(
        pattern.search(scan_text) for pattern in _PREFILTER_PATTERNS
    ):
        return True
    if _JAILBREAK_INPUT_CATEGORY in active and any(
        row[0].search(scan_text) for row in _JAILBREAK_PATTERNS
    ):
        return True
    return False


def nemo_output_active(pins: GuardrailsPin) -> bool:
    """
    Whether the out-of-process NeMo output-self-check lane runs for this config.

    True iff a ``nemo`` selector is present, ``enabled``, AND ``output_self_check``
    is on — the exact condition under which :func:`check_output_nemo` calls the
    guardrail pod. The released ``1.0.0``-``1.4.0`` configs carry no ``nemo``
    selector (``pins.nemo is None``), so this is False and the NeMo lane never
    fires — those lanes stay byte-for-byte. The router uses this to decide
    buffered delivery + span emission, mirroring ``_output_guard_active``.
    """
    return bool(pins.nemo and pins.nemo.enabled and pins.nemo.output_self_check)


def check_output_nemo(
    answer_text: str,
    chunks: list[str],
    *,
    pins: GuardrailsPin,
    nemo_client: NemoGuardClient | None,
) -> GuardOutputResult:
    """
    Config-gated OUTPUT/facts self-check via the injected NeMo pod client.

    A PURE function of the answer text, the retrieved ``chunks``, the resolved
    ``GuardrailsPin``, and an INJECTED client (described by the
    :class:`NemoGuardClient` Protocol). It imports NO transport/NeMo library — all
    HTTP detail lives in ``app.clients.nemo_guard`` (invariant I2). The client is
    reached exactly like the input classifier: injected by the router.

    Q6b (locked): this is the NeMo lane's OUTPUT + FACTS entry point; the INPUT
    lane keeps the in-house Haiku confirm-step and is untouched here.

    Args:
        answer_text: The generated answer (already through the deterministic
            regex output guard, so any redaction has been applied). Passed to the
            pod's ``self check output`` rail.
        chunks: Retrieved chunk texts (grounding evidence). Forwarded to the pod
            so ``self check facts`` can check the answer's faithfulness when the
            facts gate is on; ignored by the pod when ``check_facts`` is False.
        pins: The resolved config's ``guardrails`` block. ``nemo`` absent /
            disabled ⇒ a typed IDENTITY (``GuardOutputResult(answer_text)``), NO
            pod call — the 1.0.0-1.4.0 path. ``pins.nemo.check_facts`` is
            forwarded VERBATIM to the pod contract (the independent facts gate).
        nemo_client: The injected NeMo pod client (or ``None``).

    Returns:
        A :class:`GuardOutputResult` carrying the UNCHANGED ``answer_text`` (the
        NeMo lane never rewrites the answer — it only blocks or advises) and:
        - no decisions on a clean pass;
        - one advisory ``flag`` decision when the pod flags
          (``nemo-output-flag-v1``);
        - one LOUD fail-open ``flag`` decision when the pod is UNREACHABLE
          (``nemo-output-fail-open-v1`` — Mode B, Q2: deliver + advise, never
          block on a flaky/unreachable pod). The distinct rule id is what keeps
          the fail-open window countable rather than silent;
        plus the guard-call telemetry (pod-stamped ``model_id`` + token counts)
        for the ``guardrail_output`` span.

    Raises:
        GuardrailTripwire: On a genuine NeMo BLOCK (``unsafe`` True) — the whole
            answer is suppressed to the canned ``REFUSAL_TEXT`` by the router
            (I3); NeMo's own refusal string NEVER reaches the UI answer (only a
            terse ``rationale`` enters the decision envelope / span). Carries the
            pod-stamped ``model_id`` + token counts for the span.
        GuardMisconfiguredError: The NeMo lane is enabled but no client was
            injected — a deploy/wiring error, raised LOUDLY (→ 500), NOT masked
            as a silent refusal (mirrors ``check_input``).

    """
    if not nemo_output_active(pins):
        return GuardOutputResult(answer_text=answer_text)

    # Misconfiguration (lane enabled but no client wired) is a DEPLOY error, NOT a
    # per-request pod failure. Raise it LOUDLY so a wiring bug can never masquerade
    # as a silent fail-open flag on every answer. Deliberately OUTSIDE the
    # fail-open try below.
    if nemo_client is None:
        raise GuardMisconfiguredError(
            "guardrails.nemo is enabled but no NeMo guardrail client was injected "
            "— the output self-check lane cannot run."
        )

    check_facts = bool(pins.nemo and pins.nemo.check_facts)

    # Output/facts fail OPEN (Q2): a transport failure / non-2xx from the pod
    # (unreachable, timeout, HTTP error) must NEVER nuke a valid legal answer.
    # Deliver the answer carrying an advisory flag decision instead of blocking.
    # (The pod also fails open internally; this handles pod-unreachable, the case
    # the pod itself cannot signal.) Broad catch mirrors ``check_input``'s
    # fail-policy ownership; GuardrailTripwire is raised AFTER this block, so it
    # is never swallowed here.
    try:
        verdict = nemo_client.check_output(answer_text, chunks, check_facts=check_facts)
    except Exception:  # noqa: BLE001 - fail OPEN on a flaky/unreachable guard pod
        # LOUD, not silent: its own rule id (not the advisory's) marks the window
        # in which answers were delivered UNGUARDED.
        return GuardOutputResult(
            answer_text=answer_text,
            decisions=(_nemo_output_fail_open_decision(_NEMO_FAIL_OPEN_RATIONALE),),
        )

    if verdict.unsafe:
        # Genuine block: reuse the SAME honest 200-refusal path as the regex
        # secrets block. The router maps this to REFUSAL_TEXT — NeMo's string is
        # never the answer (I3); only the terse rationale rides the decision/span.
        raise GuardrailTripwire(
            _nemo_output_block_decision(verdict.rationale or _NEMO_DEFAULT_BLOCK_RATIONALE),
            model_id=verdict.model_id,
            input_tokens=verdict.input_tokens,
            output_tokens=verdict.output_tokens,
        )

    decisions: tuple[GuardrailDecision, ...] = ()
    if verdict.flag:
        # Advisory (the pod's own internal fail-open, or a soft advisory): deliver
        # the answer WITH a non-block flag decision, never a block.
        decisions = (_nemo_output_flag_decision(verdict.rationale or _NEMO_ADVISORY_RATIONALE),)
    return GuardOutputResult(
        answer_text=answer_text,
        decisions=decisions,
        model_id=verdict.model_id,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
    )


def nemo_input_active(pins: GuardrailsPin) -> bool:
    """
    Whether the out-of-process NeMo INPUT self-check lane runs for this config.

    True iff a ``nemo`` selector is present, ``enabled``, AND ``input_self_check``
    is on — the exact condition under which :func:`check_input_nemo` may call the
    guardrail pod's ``self_check_input`` rail. The in-house ``1.0.0``-``1.4.0``
    configs leave ``input_self_check`` OFF (or carry no ``nemo`` selector), so
    this is False and the input lane stays on the in-house Haiku confirm-step —
    those lanes are byte-for-byte. The nemo-all ``1.8.0`` config turns it on.
    """
    return bool(pins.nemo and pins.nemo.enabled and pins.nemo.input_self_check)


def nemo_prefilter_hit(question: str, pins: GuardrailsPin) -> bool:
    """
    Report whether the NeMo input lane is active AND the FREE cost gate matches.

    The cost gate is the FULL orchestrator regex pre-filter
    (:data:`_NEMO_INPUT_PREFILTER_CATEGORIES` — prompt_leak + jailbreak +
    unicode_evasion, incl. invisible-char strip + BIDI), run INDEPENDENTLY of the
    in-house ``input_categories`` decision gate (empty on ``1.8.0``). A True
    result means the pod ``self_check_input`` call WILL run for this question; a
    benign MISS makes ZERO paid pod calls. The router uses this to open the
    ``guardrail_input`` span only around an actual pod call.
    """
    return nemo_input_active(pins) and _prefilter_hit(
        question, _NEMO_INPUT_PREFILTER_CATEGORIES
    )


def _nemo_input_block_decision(rationale: str) -> GuardrailDecision:
    """Build the NeMo input-lane ``block`` decision (honest 200 refusal path, I3)."""
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="block",
        category=_NEMO_CATEGORY,
        rule_id=_NEMO_INPUT_BLOCK_RULE_ID,
        rationale=rationale,
    )


def check_input_nemo(
    question: str,
    *,
    pins: GuardrailsPin,
    nemo_client: NemoGuardClient | None,
) -> GuardInputResult:
    """
    Config-gated INPUT self-check via the injected NeMo pod client (the ``1.8.0`` lane).

    A PURE function of the question, the resolved ``GuardrailsPin``, and an
    INJECTED client (the :class:`NemoGuardClient` Protocol). It imports NO
    transport/NeMo library — all HTTP detail lives in ``app.clients.nemo_guard``
    (invariant I2). Structurally mirrors :func:`check_output_nemo`.

    The orchestrator's regex pre-filter is the FREE cost gate: a benign question
    (pre-filter MISS) returns a typed IDENTITY with ZERO paid pod calls; only a
    pre-filter HIT forwards the question to the pod. The pre-filter DETECTS on
    normalized text (invisible-char strip + BIDI) but the RAW ``question`` is
    forwarded to ``self_check_input`` — the adversarial payload is NEVER
    sanitized before the pod's LLM judge sees it. The pod's verdict REPLACES the
    in-house Haiku confirm-step on this config.

    Args:
        question: The user turn (RAW — forwarded verbatim to the pod).
        pins: The resolved config's ``guardrails`` block. ``nemo`` absent /
            disabled / ``input_self_check`` off ⇒ a typed IDENTITY
            (``GuardInputResult(question)``), NO pod call — the in-house 1.0.0-1.4.0 path.
        nemo_client: The injected NeMo pod client (or ``None``).

    Returns:
        A :class:`GuardInputResult` carrying the UNCHANGED ``question`` when
        allowed (lane off, pre-filter miss, or pre-filter hit cleared SAFE by the
        pod), plus the pod-stamped ``model_id`` + token counts when the pod ran.

    Raises:
        GuardrailTripwire: On a pre-filter hit the pod flags UNSAFE, OR any pod
            transport failure on that (already suspicious) input — the INPUT lane
            fails SAFE/BLOCK (Mode B), mirroring the in-house classifier's
            fail-safe on a flagged input. Turned into a 200 canned refusal by the
            router (never a 5xx, I3); the pod's own string never becomes the
            answer.
        GuardMisconfiguredError: The lane is enabled but no client was injected —
            a deploy/wiring error, raised LOUDLY (→ 500), NOT masked as a silent
            refusal (mirrors ``check_input`` / ``check_output_nemo``).

    """
    if not nemo_input_active(pins):
        return GuardInputResult(question=question)
    # FREE cost gate: a benign pre-filter MISS never reaches the pod (zero paid
    # calls on normal traffic) — the accepted residual (unchanged from today).
    if not _prefilter_hit(question, _NEMO_INPUT_PREFILTER_CATEGORIES):
        return GuardInputResult(question=question)

    # Misconfiguration (lane enabled but no client wired) is a DEPLOY error, NOT a
    # per-request pod failure. Raise it LOUDLY so a wiring bug can never masquerade
    # as a silent fail-safe refusal of every flagged query. OUTSIDE the fail-safe
    # try below.
    if nemo_client is None:
        raise GuardMisconfiguredError(
            "guardrails.nemo.input_self_check is enabled but no NeMo guardrail "
            "client was injected — the input self-check lane cannot run."
        )

    # Suspicious candidate: the pod's `self_check_input` confirms or clears it. A
    # transport failure on this already-flagged input fails SAFE/BLOCK (Mode B) —
    # no verdict, so no token counts. Broad catch mirrors ``check_input``'s
    # fail-policy ownership; GuardrailTripwire is raised AFTER this block.
    try:
        verdict = nemo_client.check_input(question)
    except Exception as exc:  # noqa: BLE001 — fail SAFE: pod failure on a flagged input ⇒ block
        raise GuardrailTripwire(
            _nemo_input_block_decision(_NEMO_INPUT_FAIL_SAFE_RATIONALE)
        ) from exc

    if verdict.unsafe:
        raise GuardrailTripwire(
            _nemo_input_block_decision(
                verdict.rationale or _NEMO_INPUT_DEFAULT_BLOCK_RATIONALE
            ),
            model_id=verdict.model_id,
            input_tokens=verdict.input_tokens,
            output_tokens=verdict.output_tokens,
        )
    return GuardInputResult(
        question=question,
        model_id=verdict.model_id,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
    )
