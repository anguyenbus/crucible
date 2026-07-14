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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from app.schemas.envelope import GuardrailDecision
from app.schemas.pipeline_config import GuardrailsPin

# The single canned refusal the Chainlit UI renders verbatim on a block.
REFUSAL_TEXT: Final[str] = "I'm sorry, but I can't help with that request."

# Guard identity for the prompt-leak/injection class (this slice's only class).
_INPUT_STAGE: Final[str] = "input"
_BLOCK_CATEGORY: Final[str] = "prompt_leak"
_BLOCK_RULE_ID: Final[str] = "prompt-leak-v1"
_FAIL_SAFE_RATIONALE: Final[str] = (
    "guard classifier unavailable on a pre-filter-flagged input — failing safe"
)
_DEFAULT_BLOCK_RATIONALE: Final[str] = "system-prompt extraction attempt"

# Input-guard detector sub-classes selectable via ``pins.input_categories``.
# ``prompt_leak`` is the shipped ``_PREFILTER_PATTERNS`` set (unchanged);
# ``jailbreak`` and ``unicode_evasion`` are the second-slice additions, inert
# unless a config opts in (so ``[prompt_leak]``-only configs stay byte-for-byte).
_PROMPT_LEAK_INPUT_CATEGORY: Final[str] = "prompt_leak"
_JAILBREAK_INPUT_CATEGORY: Final[str] = "jailbreak"
_UNICODE_EVASION_INPUT_CATEGORY: Final[str] = "unicode_evasion"

# Output-guard detector classes selectable via ``pins.output_categories`` and
# the fixed decision identities each produces.
_OUTPUT_STAGE: Final[str] = "output"
_SECRETS_CATEGORY: Final[str] = "secrets"
_PII_CATEGORY: Final[str] = "pii"
_OUTPUT_SECRETS_RULE_ID: Final[str] = "output-secrets-v1"
_OUTPUT_PII_REDACT_RULE_ID: Final[str] = "output-pii-redact-v1"
_OUTPUT_PII_FLAG_RULE_ID: Final[str] = "output-pii-flag-v1"


class ClassifierVerdict(Protocol):
    """Structural shape of the injected classifier's plain-data verdict."""

    unsafe: bool
    rationale: str | None
    input_tokens: int | None
    output_tokens: int | None


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


class GuardClassifier(Protocol):
    """
    Structural type of the injected Bedrock Haiku guard classifier.

    Matches ``app.clients.guardrail.GuardClassifier`` WITHOUT importing it — the
    stage stays free of any boto3-carrying module (``stages-pure``). The client
    is injected by the router exactly like ``bedrock``/``search``.
    """

    def classify(self, question: str, *, model_id: str) -> ClassifierVerdict:
        """Classify one user turn for system-prompt-leakage / injection."""
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
_SECRET_PATTERNS: Final[tuple[tuple[re.Pattern[str], str, float], ...]] = tuple(
    (re.compile(pattern), label, confidence)
    for pattern, label, confidence in (
        (r"sk-ant-[a-zA-Z0-9\-_]{40,}", "anthropic_api_key", 0.95),
        (r"AKIA[0-9A-Z]{16}", "aws_access_key", 0.95),
        (r"sk-(?:proj-)?[a-zA-Z0-9\-_]{40,}", "openai_api_key", 0.90),
        (r"AIza[0-9A-Za-z\-_]{35}", "google_api_key", 0.95),
        (r"gh[posur]_[0-9a-zA-Z]{36}", "github_token", 0.99),
        (
            r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24}",
            "slack_token",
            0.95,
        ),
        (r"hf_[a-zA-Z0-9]{34}", "huggingface_token", 0.95),
        (
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            "private_key",
            0.95,
        ),
        (
            r"eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*",
            "jwt_token",
            0.85,
        ),
        (
            r"(?i)(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://"
            r"[^\s]+:[^\s]+@[^\s]+",
            "database_url",
            0.95,
        ),
    )
)

# --- Pattern set 2: PII (OUTPUT guard, REDACT + advisory FLAG classes) --------
# Source: references/agent-learning-kit/python/fi/evals/guardrails/scanners/regex.py
# (COMMON_PATTERNS: credit_card, ssn, passport, mrn, email, phone_us).
# Each row carries a per-rule ACTION so ``check_output`` can branch redact-vs-flag:
#   "redact"  -> mask the span in place (credit_card / ssn / passport / mrn)
#   "flag"    -> advisory only, answer unchanged (email / phone)
# The SSN rule REQUIRES an explicit ``-``/space separator (tightened from the
# source's optional separator) so a plain run of 9 digits -- e.g. a statute
# number in a legal answer -- does NOT trip it (FP reduction).
# (regex, rule_label, action, source_confidence)
_PII_PATTERNS: Final[tuple[tuple[re.Pattern[str], str, str, float], ...]] = tuple(
    (re.compile(pattern), label, action, confidence)
    for pattern, label, action, confidence in (
        (r"\b(?:\d{4}[- ]?){3}\d{4}\b", "credit_card", "redact", 0.85),
        (r"\b\d{3}[- ]\d{2}[- ]\d{4}\b", "ssn", "redact", 0.80),
        (
            r"(?i)\b(?:passport)\s*[#:]?\s*[A-Z0-9]{6,9}\b",
            "passport",
            "redact",
            0.80,
        ),
        (
            r"(?i)\b(?:mrn|medical\s*record)\s*[#:]?\s*\d{6,10}\b",
            "mrn",
            "redact",
            0.85,
        ),
        (
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            "email",
            "flag",
            0.90,
        ),
        (
            # Tightened from the source's all-separators-optional form: a real
            # separator (or parenthesized area code) is REQUIRED, so a bare
            # 10-digit run — a matter/registry/reference number common in legal
            # text — no longer trips a phantom advisory flag. Space-grouped
            # numbers that genuinely look like a phone still match.
            r"(?:\+?1[-. ]?)?(?:\([0-9]{3}\)[-. ]?|[0-9]{3}[-. ])[0-9]{3}[-. ][0-9]{4}\b",
            "phone",
            "flag",
            0.75,
        ),
    )
)

# --- Pattern set 3: invisible-char / BIDI codepoints (INPUT hardening) --------
# Source: references/agent-learning-kit/python/fi/evals/guardrails/scanners/invisible_chars.py
# (INVISIBLE_CHARS, BIDI_CHARS). The large homoglyph confusable tables in that
# file are higher-FP and deliberately SKIPPED in this slice.
#   _INVISIBLE -> zero-width / invisible codepoints stripped BEFORE the keyword
#                 pre-filter runs, so ``ig<ZWSP>nore previous`` no longer bypasses.
#   _BIDI      -> BIDI override codepoints; U+202E is the dangerous one. Their
#                 presence is a standalone pre-filter HIT.
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


def _block_decision(rationale: str) -> GuardrailDecision:
    """Build the structured ``block`` decision for the prompt-leak class."""
    return GuardrailDecision(
        stage=_INPUT_STAGE,
        decision="block",
        category=_BLOCK_CATEGORY,
        rule_id=_BLOCK_RULE_ID,
        rationale=rationale,
    )


def _output_block_decision(rationale: str) -> GuardrailDecision:
    """Build the output-stage secrets ``block`` decision (mirrors ``_block_decision``)."""
    return GuardrailDecision(
        stage=_OUTPUT_STAGE,
        decision="block",
        category=_SECRETS_CATEGORY,
        rule_id=_OUTPUT_SECRETS_RULE_ID,
        rationale=rationale,
    )


def _output_redact_decision(rationale: str) -> GuardrailDecision:
    """Build the single output-stage PII ``transform`` (redaction) decision."""
    return GuardrailDecision(
        stage=_OUTPUT_STAGE,
        decision="transform",
        category=_PII_CATEGORY,
        rule_id=_OUTPUT_PII_REDACT_RULE_ID,
        rationale=rationale,
    )


def _output_flag_decision(rationale: str) -> GuardrailDecision:
    """Build the single output-stage advisory PII ``flag`` decision (answer unchanged)."""
    return GuardrailDecision(
        stage=_OUTPUT_STAGE,
        decision="flag",
        category=_PII_CATEGORY,
        rule_id=_OUTPUT_PII_FLAG_RULE_ID,
        rationale=rationale,
    )


def _format_rule_counts(counts: dict[str, int]) -> str:
    """Render a deterministic per-rule count summary (e.g. ``ssn=2, credit_card=1``)."""
    return ", ".join(f"{label}={counts[label]}" for label in counts)


def _redaction_token(rule_label: str) -> str:
    """Fixed, deterministic mask token for a redactable PII span."""
    return f"‹redacted:{rule_label}›"


def _luhn_ok(candidate: str) -> bool:
    """
    Luhn checksum over the digits of ``candidate`` (the standard card-number check).

    Gates ``credit_card`` redaction: a 16-digit run that is NOT a valid card
    number — a reference / control / exhibit number common in legal text — fails
    Luhn and is left INTACT rather than destructively masked (FP reduction).
    """
    digits = [int(ch) for ch in candidate if ch.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


# Redact rules that fire ONLY when the matched text ALSO passes a validator.
# credit_card requires a valid Luhn checksum so a 16-digit non-card is not masked.
_REDACT_VALIDATORS: Final[dict[str, Callable[[str], bool]]] = {"credit_card": _luhn_ok}


def _subn_validated(
    pattern: re.Pattern[str], token: str, validator: Callable[[str], bool], text: str
) -> tuple[str, int]:
    """Like ``pattern.subn(token, text)`` but replaces ONLY matches passing ``validator``."""
    replaced = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal replaced
        if validator(match.group(0)):
            replaced += 1
            return token
        return match.group(0)

    return pattern.sub(_replace, text), replaced


# Citation-marker grammar, mirroring ``citation_builder._MARKER_RE``. Redaction
# skips these spans so a ``[chunk_id]`` whose id contains a PII-shaped run (e.g. a
# 16-digit doc id the ``credit_card`` rule would match, or a 10-digit id the
# ``phone`` rule would match) is preserved VERBATIM — otherwise the marker would
# be mangled in place and the downstream citation builder, which runs AFTER
# redaction over the same text, would fail to resolve it and silently DROP the
# citation (miscounting it as a hallucinated marker).
_CITATION_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"\[[^\[\]]+\]")


def _split_citation_markers(text: str) -> list[tuple[bool, str]]:
    """
    Split ``text`` into ordered ``(is_marker, segment)`` parts.

    ``is_marker`` True marks a ``[chunk_id]`` citation marker to be preserved
    verbatim (never scanned or masked); False marks scannable text between
    markers. Concatenating the segments reproduces ``text`` exactly, so it is
    offset-preserving: citation ``claim_span`` offsets built over the redacted
    result stay consistent with the delivered answer.
    """
    parts: list[tuple[bool, str]] = []
    last = 0
    for match in _CITATION_MARKER_RE.finditer(text):
        if match.start() > last:
            parts.append((False, text[last : match.start()]))
        parts.append((True, match.group(0)))
        last = match.end()
    if last < len(text):
        parts.append((False, text[last:]))
    return parts


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


def prefilter_hit(question: str, pins: GuardrailsPin) -> bool:
    """
    Report whether the guard is enabled AND an active pre-filter class matches.

    A True result means the classifier WILL run for this question. The router
    uses this to open the ``guardrail_input`` LLM span only around an actual
    classifier call (so a benign pre-filter miss adds no span), then calls
    :func:`check_input` as usual. ``check_input`` re-checks the pre-filter itself,
    so it stays fully self-contained (callable without the router's gate). The
    active pattern set + normalization are selected from ``pins.input_categories``.
    """
    return pins.enabled and _prefilter_hit(question, pins.input_categories)


def check_input(
    question: str,
    *,
    pins: GuardrailsPin,
    classifier: GuardClassifier | None,
) -> GuardInputResult:
    """
    Config-gated system-prompt-leakage input guard (allows with telemetry, or blocks).

    Args:
        question: The user turn (the current question, pre-rewrite).
        pins: The resolved config's ``guardrails`` block. ``enabled=False`` ⇒
            a typed IDENTITY: return the ``question`` unchanged, NO classifier
            call (the eval/1.0.0/1.1.0/1.2.0 path — behavior identical to today).
            ``pins.input_categories`` selects the active pre-filter classes.
        classifier: The injected Bedrock Haiku guard classifier (or ``None``).

    Returns:
        A :class:`GuardInputResult` carrying the unchanged ``question`` when
        allowed (gate off, pre-filter miss, or pre-filter hit classified SAFE);
        the classifier-call telemetry (model id + token counts) is populated
        only when the classifier actually ran.

    Raises:
        GuardrailTripwire: On a pre-filter hit that the classifier flags UNSAFE,
            OR any classifier error on that (already suspicious) input — fail
            SAFE. Carries the guard call's telemetry for the span. Normal traffic
            never calls the classifier, so an outage can neither refuse benign
            queries nor add cost to them.
        GuardMisconfiguredError: Guard enabled but no classifier/model wired.

    """
    if not pins.enabled:
        return GuardInputResult(question=question)
    if not _prefilter_hit(question, pins.input_categories):
        return GuardInputResult(question=question)

    # Misconfiguration (guard enabled but no classifier client / model id wired)
    # is a DEPLOY error, NOT a per-request classifier failure. Raise it LOUDLY
    # (→ 500) so a wiring bug can never masquerade as a silent refusal of every
    # flagged query. Deliberately OUTSIDE the fail-safe try below.
    if classifier is None or not pins.classifier_model_id:
        raise GuardMisconfiguredError(
            "guardrails.enabled is true but no classifier client / model id was "
            "injected — the input guard cannot run."
        )

    # Suspicious candidate: the classifier confirms or clears it. A RUNTIME
    # classifier failure (throttle, ClientError, unparseable verdict) on this
    # already-flagged input fails SAFE (BLOCK) — no verdict, so no token counts.
    try:
        verdict = classifier.classify(question, model_id=pins.classifier_model_id)
    except Exception as exc:  # noqa: BLE001 — fail-safe: runtime classifier failure ⇒ block
        raise GuardrailTripwire(
            _block_decision(_FAIL_SAFE_RATIONALE), model_id=pins.classifier_model_id
        ) from exc

    if verdict.unsafe:
        raise GuardrailTripwire(
            _block_decision(verdict.rationale or _DEFAULT_BLOCK_RATIONALE),
            model_id=pins.classifier_model_id,
            input_tokens=verdict.input_tokens,
            output_tokens=verdict.output_tokens,
        )
    return GuardInputResult(
        question=question,
        model_id=pins.classifier_model_id,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
    )


def check_output(answer_text: str, *, pins: GuardrailsPin) -> GuardOutputResult:
    """
    Config-gated, PURE, deterministic output PII/secrets guard (regex-only, NO model).

    Args:
        answer_text: The generated answer (with any ``[chunk_id]`` citation
            markers still embedded — redaction leaves them intact).
        pins: The resolved config's ``guardrails`` block. An empty
            ``output_categories`` ⇒ a typed IDENTITY:
            ``GuardOutputResult(answer_text, ())`` — no scan, the released
            ``1.0.0``–``1.3.0`` path. ``output_categories`` selects the active
            scan classes (``secrets`` / ``pii``) and is the SOLE gate: the output
            guard is pure regex with no model dependency, so it is INDEPENDENT of
            ``enabled`` / the input classifier (a config may run the output guard
            with ``enabled=False`` and no classifier wired).

    Returns:
        A :class:`GuardOutputResult` carrying the possibly-redacted ``answer_text``
        (redactable PII spans masked left-to-right with a fixed token) and the
        accumulated non-block decisions: at most one ``transform`` (PII redaction,
        per-rule count) and one ``flag`` (advisory email/phone, count).

    Raises:
        GuardrailTripwire: On a ``secrets`` hit (only when ``"secrets"`` is in
            ``output_categories``) — the whole answer is suppressed to the canned
            refusal. Regex-only, so NO model/token telemetry on the tripwire.
    """
    if not pins.output_categories:
        return GuardOutputResult(answer_text=answer_text, decisions=())

    active = frozenset(pins.output_categories)

    # secrets ⇒ BLOCK: the whole answer is suppressed. Checked first so a
    # credential-bearing answer never has PII merely masked and delivered. Scans
    # ONLY the text BETWEEN [chunk_id] markers (like the PII scan): a marker is a
    # machine-generated chunk id, not model prose, so a credential-shaped doc id
    # must never trigger a false full-answer block.
    if _SECRETS_CATEGORY in active:
        for is_marker, segment in _split_citation_markers(answer_text):
            if is_marker:
                continue
            for pattern, label, _confidence in _SECRET_PATTERNS:
                if pattern.search(segment):
                    raise GuardrailTripwire(_output_block_decision(label))

    decisions: list[GuardrailDecision] = []
    text = answer_text

    if _PII_CATEGORY in active:
        # Redactable PII ⇒ mask each span in place with a fixed deterministic
        # token; advisory PII (email/phone) ⇒ FLAG only. BOTH operate ONLY on the
        # text BETWEEN [chunk_id] citation markers (_split_citation_markers): a
        # marker is preserved verbatim, so a numeric-heavy chunk id can never be
        # corrupted (which would silently drop the citation downstream) and never
        # yields a phantom redaction/flag. Advisory counts run on the already-
        # redacted segment so a masked span never re-counts as a flag. Marker
        # spans are offset-preserving, so citation claim_span offsets stay valid.
        redact_counts: dict[str, int] = {}
        flag_counts: dict[str, int] = {}
        rebuilt: list[str] = []
        for is_marker, segment in _split_citation_markers(text):
            if is_marker:
                rebuilt.append(segment)  # never scanned or masked
                continue
            for pattern, label, action, _confidence in _PII_PATTERNS:
                if action != "redact":
                    continue
                validator = _REDACT_VALIDATORS.get(label)
                if validator is None:
                    segment, n = pattern.subn(_redaction_token(label), segment)
                else:
                    # e.g. credit_card: mask ONLY Luhn-valid matches; a 16-digit
                    # non-card reference number is left intact.
                    segment, n = _subn_validated(
                        pattern, _redaction_token(label), validator, segment
                    )
                if n:
                    redact_counts[label] = redact_counts.get(label, 0) + n
            for pattern, label, action, _confidence in _PII_PATTERNS:
                if action != "flag":
                    continue
                n = len(pattern.findall(segment))
                if n:
                    flag_counts[label] = flag_counts.get(label, 0) + n
            rebuilt.append(segment)
        text = "".join(rebuilt)
        # Redaction decision is appended BEFORE the flag decision so decisions[0]
        # is the transform when both fire (the router records it as primary).
        if redact_counts:
            decisions.append(_output_redact_decision(_format_rule_counts(redact_counts)))
        if flag_counts:
            decisions.append(_output_flag_decision(_format_rule_counts(flag_counts)))

    return GuardOutputResult(answer_text=text, decisions=tuple(decisions))
