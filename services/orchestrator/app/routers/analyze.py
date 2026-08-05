"""
``POST /analyze``: single-document facts extraction + summary (NON-RAG).

The caller supplies the document text; this route runs the pinned generator
ONCE over it and returns a structured ``{summary, facts}``. There is NO
retrieval, NO citation build, and NO guardrail lane here — it is a bounded,
read-only analysis of text the caller already owns, so it stays available even
when OpenSearch is down. Bedrock throttle/ClientError propagate to the SAME
app-level handlers as ``/query`` (503 + Retry-After / 502 dependency:bedrock);
config-ref problems map to the same 422/404/500 as ``/query``.

Sampling is fixed low (temperature 0.0) for reproducible structured extraction,
independent of the generator pin's chat temperature. The model is asked for a
strict JSON object; parsing degrades honestly (a non-JSON reply becomes the
summary with no facts) rather than 500-ing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, Request

from app.config import resolve_pipeline_config
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse

if TYPE_CHECKING:
    from app.clients import AppClients
    from app.clients.bedrock import GenerationResult

analyze_router = APIRouter(tags=["analyze"])

# Cap the text handed to the model: uploads are bounded to ~1 MiB but we still
# bound the prompt so a large document neither blows the context window nor the
# token bill. Truncation is REPORTED back (`truncated: true`), never silent.
_MAX_INPUT_CHARS: Final[int] = 60_000
# Fixed low sampling for reproducible extraction (NOT the chat temperature).
_ANALYZE_TEMPERATURE: Final[float] = 0.0
# Enough for a short summary plus a few dozen one-line facts as JSON.
_ANALYZE_MAX_TOKENS: Final[int] = 2048

_GROUNDING: Final[str] = (
    "You are a meticulous legal document analyst. Work ONLY from what the "
    "DOCUMENT actually states — never infer, guess, or add outside knowledge."
)

_BOTH_PROMPT: Final[str] = (
    _GROUNDING
    + """ Produce two things:

1. summary: a concise, plain-English summary of the document in 2-4 sentences.
2. facts: a list of the most important discrete facts EXPLICITLY stated in the \
document — parties/names, dates, monetary amounts, obligations, defined terms, \
governing law, key clauses. Each fact is ONE short self-contained sentence. \
Return at most {max_facts} facts, most important first. If the document states \
no discrete facts, return an empty list.

Respond with ONLY a single JSON object, no prose and no code fences, of exactly \
this shape:
{{"summary": "<the summary>", "facts": ["<fact 1>", "<fact 2>"]}}

DOCUMENT:
{text}
"""
)

_FACTS_PROMPT: Final[str] = (
    _GROUNDING
    + """ Extract the most important discrete facts EXPLICITLY stated in the \
document — parties/names, dates, monetary amounts, obligations, defined terms, \
governing law, key clauses. Each fact is ONE short self-contained sentence. \
Return at most {max_facts} facts, most important first. If the document states \
no discrete facts, return an empty list.

Respond with ONLY a single JSON object, no prose and no code fences, of exactly \
this shape:
{{"facts": ["<fact 1>", "<fact 2>"]}}

DOCUMENT:
{text}
"""
)

_SUMMARY_PROMPT: Final[str] = (
    _GROUNDING
    + """ Write a concise, plain-English summary of the document in 2-4 \
sentences. Respond with ONLY the summary text — no preamble, no labels, no code \
fences.

DOCUMENT:
{text}
"""
)


def _build_prompt(mode: str, text: str, max_facts: int) -> str:
    template = {
        "facts": _FACTS_PROMPT,
        "summary": _SUMMARY_PROMPT,
        "both": _BOTH_PROMPT,
    }[mode]
    return template.format(text=text, max_facts=max_facts)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """
    Best-effort parse of the model's reply into a JSON object.

    Tries the whole reply first, then the widest ``{...}`` slice (tolerating
    stray prose or code fences the model may add). Returns ``None`` if no JSON
    object can be recovered.
    """
    stripped = raw.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def _parse_analysis(raw: str, max_facts: int) -> tuple[str, list[str]]:
    """
    Map the model reply to ``(summary, facts)``, degrading honestly.

    A reply we cannot parse as JSON becomes the summary with no facts, so the
    caller always gets SOMETHING truthful rather than a 500.
    """
    obj = _extract_json_object(raw)
    if obj is None:
        return raw.strip(), []
    summary = str(obj.get("summary", "")).strip()
    facts: list[str] = []
    raw_facts = obj.get("facts", [])
    if isinstance(raw_facts, list):
        for item in raw_facts:
            fact = str(item).strip()
            if fact:
                facts.append(fact)
    return summary, facts[:max_facts]


@analyze_router.post("/analyze", response_model=AnalyzeResponse)
def post_analyze(request: AnalyzeRequest, http_request: Request) -> AnalyzeResponse:
    """
    Analyse one document's text into a summary and/or discrete facts (non-RAG).

    ``mode`` selects the work: ``facts`` and ``summary`` each run a single
    focused generation (the UI's "Extract" / "Summarise" buttons); ``both``
    returns both from one prompt. Fields not produced by the chosen mode come
    back empty (``summary=""`` / ``facts=[]``).
    """
    resolved = resolve_pipeline_config(request.pipeline_config)
    clients: AppClients = http_request.app.state.clients

    truncated = len(request.text) > _MAX_INPUT_CHARS
    text = request.text[:_MAX_INPUT_CHARS] if truncated else request.text
    prompt = _build_prompt(request.mode, text, request.max_facts)

    generator = resolved.config.generator
    generation: GenerationResult = clients.bedrock.generate(
        prompt,
        model_id=generator.model_id,
        temperature=_ANALYZE_TEMPERATURE,
        max_tokens=_ANALYZE_MAX_TOKENS,
    )

    if request.mode == "summary":
        # Plain prose reply — the whole answer IS the summary (no JSON to parse).
        summary, facts = generation.text.strip(), []
    elif request.mode == "facts":
        _, facts = _parse_analysis(generation.text, request.max_facts)
        summary = ""
    else:
        summary, facts = _parse_analysis(generation.text, request.max_facts)

    return AnalyzeResponse(
        summary=summary,
        facts=facts,
        model_id=generation.model_id,
        truncated=truncated,
    )
