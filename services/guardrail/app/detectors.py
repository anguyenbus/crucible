"""
Pure-regex deterministic secrets / PII detector — the pod's FIRST output rail.

This is the single implementation behind BOTH the registered NeMo output rail
(``config/actions.py`` → ``config/rails/deterministic_output.co``) and the pod's
authoritative output-lane pre-pass (``app.nemo_runtime.check_output``). It runs
NO model / LLM call — it is pure ``re`` over the answer text — so it survives the
common failure mode where the paid Bedrock self-check rails throttle or error
(Mode A): the secrets / high-severity-PII block still fires at zero cost.

The pattern TABLES live in ``config/detectors.yml`` (a hashed artifact folded
into ``config_dir_digest``); this module only LOADS + COMPILES them and applies
the ported Luhn checksum. Detection behavior is byte-for-byte the orchestrator's
in-house ``_SECRET_PATTERNS`` / ``_PII_PATTERNS`` / ``_luhn_ok`` — do NOT
"improve" detection here (parity is a Phase-2 gate).

Verdict policy (deterministic, no LLM):
- ``secrets`` → BLOCK (short-circuits the paid ``self_check_output`` /
  ``self_check_facts`` LLM rails).
- high-severity PII (``credit_card`` / ``ssn`` / ``passport`` / ``mrn``) → BLOCK;
  redaction/masking is DROPPED (accepted trade — a high-sev PII answer refuses).
- low-severity PII (``email`` / ``phone``) → FLAG + deliver (does NOT block and
  does NOT short-circuit; the answer still runs the LLM rails).

The verdict is VERDICT-ONLY: this module NEVER rewrites the answer text. The
attribution it produces (``detections``: labels + counts, NO offsets) is recorded
on the response even when a block short-circuits the LLM rails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Final

import yaml

from app.contract import Detection

# Detection category labels carried on ``Detection.category`` (the CLASS that
# fired, distinct from the per-rule ``label``). Scoreable, low-cardinality.
_CATEGORY_SECRETS: Final[str] = "secrets"
_CATEGORY_PII: Final[str] = "pii"

# PII severities encoded per-entry in detectors.yml: high → BLOCK, low → FLAG.
_SEVERITY_HIGH: Final[str] = "high"
_SEVERITY_LOW: Final[str] = "low"


def _luhn_ok(candidate: str) -> bool:
    """
    Luhn checksum over the digits of ``candidate`` (the standard card-number check).

    Ported VERBATIM from ``services/orchestrator/app/orchestrator/guardrails.py``
    ``_luhn_ok``. Gates ``credit_card``: a 16-digit run that is NOT a valid card
    number — a reference / control / exhibit number common in legal text — fails
    Luhn and does NOT trip the block.
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


# Named pod-side validators referenced by a detectors.yml entry's ``validator``
# field. Only Luhn is needed this slice (credit_card); the registry is the
# by-name lookup the spec calls for so a new validator is a one-line addition.
_VALIDATORS: Final[dict[str, Callable[[str], bool]]] = {"luhn": _luhn_ok}


class DetectorConfigError(RuntimeError):
    """
    A detectors.yml entry is malformed OR a pattern fails to compile.

    Raised at pod startup (``/readyz`` fail-fast): a detector that cannot compile
    must NEVER silently no-op, so the pod reports NOT-ready rather than serving
    with a dead detector.
    """


@dataclass(frozen=True)
class _CompiledRule:
    """One compiled detector rule: its category, label, block-ness, validator."""

    category: str
    label: str
    pattern: re.Pattern[str]
    blocks: bool
    validator: Callable[[str], bool] | None

    def count(self, text: str) -> int:
        """Count matches in ``text`` (only validator-passing matches when gated)."""
        if self.validator is None:
            return len(self.pattern.findall(text))
        return sum(1 for m in self.pattern.finditer(text) if self.validator(m.group(0)))


@dataclass(frozen=True)
class ScanResult:
    """
    Outcome of a deterministic scan: the verdict + its attribution.

    ``blocked`` True ⇒ a secret or high-severity PII rule fired (the answer must
    refuse and the paid LLM rails are short-circuited); ``rationale`` names the
    first blocking rule's label. ``detections`` carries EVERY rule that fired
    (block + flag), labels + counts only, NO offsets — the verdict-only
    attribution recorded on the response even when short-circuiting.
    """

    blocked: bool
    rationale: str | None
    detections: tuple[Detection, ...] = ()

    @property
    def verdict(self) -> str:
        """A terse verdict label: ``block`` / ``flag`` / ``clean`` (for telemetry)."""
        if self.blocked:
            return "block"
        return "flag" if self.detections else "clean"


class Detectors:
    """A compiled, ready-to-run deterministic secrets/PII detector."""

    def __init__(self, rules: list[_CompiledRule]) -> None:
        self._rules = rules

    def scan(self, text: str) -> ScanResult:
        """
        Scan ``text`` once and return the deterministic verdict + attribution.

        Rules run in declared order (secrets first, then high- then low-severity
        PII) so ``rationale`` names the highest-priority firing rule. EVERY firing
        rule contributes a :class:`Detection` (labels + counts), so a block still
        records low-severity co-occurrences and a flag records all its labels.
        """
        detections: list[Detection] = []
        blocked = False
        rationale: str | None = None
        for rule in self._rules:
            n = rule.count(text)
            if not n:
                continue
            detections.append(Detection(category=rule.category, label=rule.label, count=n))
            if rule.blocks and not blocked:
                blocked = True
                rationale = rule.label
        return ScanResult(blocked=blocked, rationale=rationale, detections=tuple(detections))


def _compile_rules(raw: dict) -> list[_CompiledRule]:
    """Compile a parsed detectors.yml mapping into ordered :class:`_CompiledRule`."""
    rules: list[_CompiledRule] = []

    for entry in raw.get("secrets") or ():
        pattern = entry.get("pattern")
        label = entry.get("label")
        if not pattern or not label:
            raise DetectorConfigError(f"secrets entry missing pattern/label: {entry!r}")
        rules.append(
            _CompiledRule(
                category=_CATEGORY_SECRETS,
                label=label,
                pattern=_compile_pattern(pattern, label),
                blocks=True,  # every secret blocks
                validator=None,
            )
        )

    for entry in raw.get("pii") or ():
        pattern = entry.get("pattern")
        label = entry.get("label")
        severity = entry.get("severity")
        if not pattern or not label:
            raise DetectorConfigError(f"pii entry missing pattern/label: {entry!r}")
        if severity not in (_SEVERITY_HIGH, _SEVERITY_LOW):
            raise DetectorConfigError(
                f"pii entry {label!r} has invalid severity {severity!r} "
                f"(expected {_SEVERITY_HIGH!r} or {_SEVERITY_LOW!r})"
            )
        validator_name = entry.get("validator")
        validator = None
        if validator_name is not None:
            validator = _VALIDATORS.get(validator_name)
            if validator is None:
                raise DetectorConfigError(
                    f"pii entry {label!r} names unknown validator {validator_name!r}"
                )
        rules.append(
            _CompiledRule(
                category=_CATEGORY_PII,
                label=label,
                pattern=_compile_pattern(pattern, label),
                blocks=(severity == _SEVERITY_HIGH),
                validator=validator,
            )
        )

    if not rules:
        raise DetectorConfigError("detectors.yml compiled to zero rules (empty table)")
    return rules


def _compile_pattern(pattern: str, label: str) -> re.Pattern[str]:
    """Compile ONE pattern; a compile failure is a fatal (``/readyz``) config error."""
    try:
        return re.compile(pattern)
    except re.error as exc:  # a broken detector must never silently no-op
        raise DetectorConfigError(
            f"detector pattern for {label!r} failed to compile: {exc}"
        ) from exc


def detectors_path(config_dir: str | Path) -> Path:
    """The detectors.yml location inside the pod's NeMo ``config/`` directory."""
    return Path(config_dir) / "detectors.yml"


def load_detectors(config_dir: str | Path) -> Detectors:
    """
    Load + COMPILE every pattern from ``config_dir/detectors.yml``.

    Called at pod startup: any missing file, malformed entry, or non-compiling
    pattern raises :class:`DetectorConfigError`, which the lifespan turns into a
    NOT-ready pod (``/readyz`` fail-fast). A compiled detector is reused per
    request (no per-request recompile).

    Raises:
        DetectorConfigError: The file is absent/unreadable, an entry is malformed,
            a validator name is unknown, or any pattern fails to compile.
    """
    path = detectors_path(config_dir)
    if not path.is_file():
        raise DetectorConfigError(f"detectors.yml not found under {config_dir}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DetectorConfigError(f"detectors.yml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise DetectorConfigError("detectors.yml must be a mapping (secrets:/pii:)")
    return Detectors(_compile_rules(raw))


@lru_cache(maxsize=1)
def load_default_detectors() -> Detectors:
    """
    Compiled detectors for the shipping ``config/`` (used by the NeMo action).

    Cached per process. The pod's authoritative path injects an explicit
    :class:`Detectors` (built in lifespan); this is the fallback the registered
    NeMo Colang action resolves when it runs inside ``LLMRails.generate``.
    """
    from app.config_digest import default_config_dir

    return load_detectors(default_config_dir())
