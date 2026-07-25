"""
Pure-regex deterministic INJECTION scanner — the pod's ``/check/chunks`` lane.

This is the ingest-time, per-chunk corpus-poisoning guard. Ingestion sends the
chunks a document was split into (AFTER chunking, BEFORE indexing); the scanner
labels each chunk ``safe`` / ``unsafe`` and — because ONE unsafe chunk rejects
the WHOLE document (decision 2026-07-25) — reports FORENSIC attribution for every
hit so the frontend can show the officer *which part* is unsafe and *why*.

It runs NO model / LLM call — pure ``re`` over each chunk's text, one pass per
compiled rule — so, like the secrets detector (``app.detectors``), the scan holds
at zero cost even when Bedrock is throttling. The pattern TABLE lives in
``config/injections.yml`` (a hashed artifact folded into ``config_dir_digest``);
this module only LOADS + COMPILES it and produces the verdict.

Forensic contract (decision 2026-07-25 "show everything important to forensic"):
each hit carries its ``category`` / ``label`` / ``severity``, the exact
``char_start``/``char_end`` span, the matched text, and a surrounding context
window — with invisible/BIDI/zero-width codepoints ESCAPED to ``\\uXXXX`` so a hit
that is by nature unprintable is still legible in the alert. This is a deliberate
carve-out from the answer lane's "verdict-only, NO offsets" rule (``Detection``):
that rule exists so the pod never rewrites an *answer*; here the pod is helping an
operator triage an *uploaded document*, where seeing the offending span IS the
job. The scanner still never mutates the chunk — it only reports.

Verdict policy (deterministic, no LLM):
- a chunk with ANY hit → ``unsafe``;
- a document with ANY unsafe chunk → ``safe=False`` (ingestion rejects it whole).
Severity (``high`` / ``medium``) is forensic ranking metadata; it does NOT gate
the verdict (any hit is unsafe).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

from app.contract import (
    CheckChunksRequest,
    CheckChunksResponse,
    ChunkDetection,
    ChunkVerdict,
)

# Only recognised top-level key in injections.yml (parallels detectors.yml's
# single ``secrets:`` table — an unknown key is a hard error, never a silent skip).
_TABLE_KEY: Final[str] = "injections"
_VALID_SEVERITIES: Final[frozenset[str]] = frozenset({"high", "medium"})
# Characters up to ±this many either side of a match are shown as forensic
# context (the alert wants the surrounding sentence, not the whole chunk).
_CONTEXT_RADIUS: Final[int] = 48
# Response-amplification caps. A chunk is UNSAFE on the FIRST hit, so exhaustive
# enumeration adds no verdict value — but an attacker-supplied chunk stuffed with
# thousands of zero-width chars would otherwise yield one detection (each with a
# ~96-char context window) PER character, ballooning the pod + ingestion response.
# Bounded per rule and per chunk; the verdict is unchanged, the evidence is a
# representative sample.
_MAX_MATCHES_PER_RULE: Final[int] = 25
_MAX_DETECTIONS_PER_CHUNK: Final[int] = 100


class ChunkScanConfigError(RuntimeError):
    """
    injections.yml is malformed, carries an unknown table, or fails to compile.

    Raised at pod startup (``/readyz`` fail-fast): a scanner that cannot compile
    must NEVER silently no-op, so the pod reports NOT-ready rather than serving
    with a dead corpus-poisoning guard.
    """


@dataclass(frozen=True)
class _CompiledInjectionRule:
    """One compiled injection rule: its category, label, severity, pattern."""

    category: str
    label: str
    severity: str
    pattern: re.Pattern[str]


def _forensic_text(text: str) -> str:
    """
    Render ``text`` for an alert, ESCAPING invisible/control codepoints to \\uXXXX.

    A BIDI override, zero-width space, or tag char is by nature unprintable, so
    the raw matched span would be blank in the UI — defeating the "which part is
    unsafe" requirement. Printable characters pass through unchanged; anything in
    a control/format/separator class (and the ASCII controls) becomes an explicit
    escape so the operator SEES what was smuggled.
    """
    import unicodedata

    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        category = unicodedata.category(ch)
        # Cc=control, Cf=format (ZWSP/BIDI/tag), Cs=surrogate, Co=private-use,
        # Zl/Zp=line/para separators. Keep normal spaces/tabs/newlines readable.
        if ch in ("\n", "\t", " "):
            out.append(ch)
        elif category in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp"):
            out.append(f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}")
        else:
            out.append(ch)
    return "".join(out)


class ChunkScanner:
    """A compiled, ready-to-run deterministic injection scanner."""

    def __init__(self, rules: list[_CompiledInjectionRule]) -> None:
        self._rules = rules

    def scan(self, text: str) -> list[ChunkDetection]:
        """
        Scan ONE chunk and return every injection hit with forensic attribution.

        Rules run in DECLARED order; within a rule, matches are reported in
        left-to-right order. Matches are not deduplicated (an operator triaging a
        rejected document wants to see the hits), but the evidence is bounded per
        rule (:data:`_MAX_MATCHES_PER_RULE`) and per chunk
        (:data:`_MAX_DETECTIONS_PER_CHUNK`) so an invisible-char-stuffed chunk
        cannot amplify the response — the verdict (unsafe on any hit) is unchanged.
        """
        detections: list[ChunkDetection] = []
        for rule in self._rules:
            rule_hits = 0
            for m in rule.pattern.finditer(text):
                if rule_hits >= _MAX_MATCHES_PER_RULE:
                    break
                rule_hits += 1
                start, end = m.start(), m.end()
                ctx_start = max(0, start - _CONTEXT_RADIUS)
                ctx_end = min(len(text), end + _CONTEXT_RADIUS)
                detections.append(
                    ChunkDetection(
                        category=rule.category,
                        label=rule.label,
                        severity=rule.severity,
                        char_start=start,
                        char_end=end,
                        matched_excerpt=_forensic_text(m.group(0)),
                        context=_forensic_text(text[ctx_start:ctx_end]),
                    )
                )
                if len(detections) >= _MAX_DETECTIONS_PER_CHUNK:
                    return detections
        return detections


def check_chunks(scanner: ChunkScanner, request: CheckChunksRequest) -> CheckChunksResponse:
    """
    Evaluate every chunk of a document and assemble the whole-document verdict.

    Any chunk with a hit is ``unsafe``; any unsafe chunk makes the DOCUMENT
    ``safe=False`` (ingestion must reject it whole and index nothing). The result
    carries per-chunk forensic detail plus roll-up counts for the alert.
    """
    results: list[ChunkVerdict] = []
    unsafe_chunk_count = 0
    detection_count = 0
    for chunk in request.chunks:
        detections = scanner.scan(chunk.text)
        detection_count += len(detections)
        is_unsafe = bool(detections)
        if is_unsafe:
            unsafe_chunk_count += 1
        results.append(
            ChunkVerdict(
                chunk_id=chunk.id,
                ordinal=chunk.ordinal,
                verdict="unsafe" if is_unsafe else "safe",
                detections=detections,
            )
        )
    safe = unsafe_chunk_count == 0
    return CheckChunksResponse(
        safe=safe,
        verdict="clean" if safe else "unsafe",
        document_id=request.document_id,
        source_ref=request.source_ref,
        chunk_count=len(request.chunks),
        unsafe_chunk_count=unsafe_chunk_count,
        detection_count=detection_count,
        results=results,
        engine="deterministic",
    )


def _compile_rules(raw: dict) -> list[_CompiledInjectionRule]:
    """
    Compile a parsed injections.yml mapping into ordered rules.

    ``injections:`` is the ONLY recognised top-level key; any other key is a hard
    :class:`ChunkScanConfigError`. Per entry, ``category`` / ``label`` /
    ``pattern`` are mandatory and ``severity`` must be ``high`` or ``medium``.
    """
    unknown = sorted(set(raw) - {_TABLE_KEY})
    if unknown:
        raise ChunkScanConfigError(
            f"injections.yml has unsupported top-level key(s) {unknown} — "
            f"{_TABLE_KEY!r} is the only recognised table"
        )

    rules: list[_CompiledInjectionRule] = []
    for entry in raw.get(_TABLE_KEY) or ():
        category = entry.get("category")
        label = entry.get("label")
        pattern = entry.get("pattern")
        severity = entry.get("severity", "high")
        if not category or not label or not pattern:
            raise ChunkScanConfigError(
                f"injection entry missing category/label/pattern: {entry!r}"
            )
        if severity not in _VALID_SEVERITIES:
            raise ChunkScanConfigError(
                f"injection entry {label!r} has invalid severity {severity!r} "
                f"(expected one of {sorted(_VALID_SEVERITIES)})"
            )
        rules.append(
            _CompiledInjectionRule(
                category=category,
                label=label,
                severity=severity,
                pattern=_compile_pattern(pattern, label),
            )
        )

    if not rules:
        raise ChunkScanConfigError("injections.yml compiled to zero rules (empty table)")
    return rules


def _compile_pattern(pattern: str, label: str) -> re.Pattern[str]:
    """Compile ONE pattern; a compile failure is a fatal (``/readyz``) config error."""
    try:
        return re.compile(pattern)
    except re.error as exc:  # a broken scanner must never silently no-op
        raise ChunkScanConfigError(
            f"injection pattern for {label!r} failed to compile: {exc}"
        ) from exc


def injections_path(config_dir: str | Path) -> Path:
    """The injections.yml location inside the pod's NeMo ``config/`` directory."""
    return Path(config_dir) / "injections.yml"


def load_chunk_scanner(config_dir: str | Path) -> ChunkScanner:
    """
    Load + COMPILE every pattern from ``config_dir/injections.yml``.

    Called at pod startup: any missing file, malformed entry, or non-compiling
    pattern raises :class:`ChunkScanConfigError`, which the lifespan turns into a
    NOT-ready pod (``/readyz`` fail-fast). The compiled scanner is reused per
    request (no per-request recompile).
    """
    path = injections_path(config_dir)
    if not path.is_file():
        raise ChunkScanConfigError(f"injections.yml not found under {config_dir}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ChunkScanConfigError(f"injections.yml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ChunkScanConfigError("injections.yml must be a mapping (injections:)")
    return ChunkScanner(_compile_rules(raw))


@lru_cache(maxsize=1)
def load_default_chunk_scanner() -> ChunkScanner:
    """Compiled scanner for the shipping ``config/`` (cached per process)."""
    from app.config_digest import default_config_dir

    return load_chunk_scanner(default_config_dir())
