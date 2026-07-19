"""
``POST /compare``: two-document CONTRADICTION detection (NON-RAG, LangChain).

The caller supplies both documents' text; this route runs a *decompose-then-
verify* pipeline (the reusable core of the CLAIRE agent, EMNLP 2025) over the
pinned generator model via ``langchain-aws``'s ``ChatBedrockConverse``:

  1. DECOMPOSE — each document is broken into atomic, self-contained claims,
     each carrying its EXACT supporting quote (one structured call per document,
     run concurrently as a batch).
  2. CROSS-EXAMINE — the two claim lists are compared for genuine
     contradictions; each is classified into one of six types and carries the
     exact conflicting quote from EACH document (one structured call).

There is NO retrieval and NO OpenSearch here, so it stays available even when
retrieval is down. Sampling is fixed low (temperature 0.0) for reproducible
structured extraction. Bedrock throttle/ClientError propagate to the SAME
app-level handlers as ``/query`` (503 + Retry-After / 502 dependency:bedrock);
config-ref problems map to the same 422/404/500.

Honest degradation: a stage whose structured output cannot be produced/parsed
degrades to an empty result (no claims -> no contradictions) rather than 500 —
the caller always gets a truthful, well-typed answer. Genuine infrastructure
faults (a Bedrock ``ClientError``) still propagate to the app-level handlers.

WHY this lives in ``app.routers`` and not ``app.orchestrator``: the ``langchain``
stack pulls ``boto3`` transitively, which the ``stages-pure`` import-linter
contract forbids inside ``app.orchestrator.**``. The pipeline core stays pure;
this boundary route owns the LangChain client, exactly as ``app.clients`` owns
the raw Bedrock client for ``/query`` and ``/analyze``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.config import resolve_pipeline_config
from app.schemas.compare import (
    CompareRequest,
    CompareResponse,
    Contradiction,
    ContradictionReport,
)

if TYPE_CHECKING:
    from app.config import ResolvedPipelineConfig

compare_router = APIRouter(tags=["compare"])

# Cap the text handed to the model PER DOCUMENT so two large documents together
# neither blow the context window nor the token bill. Truncation is REPORTED
# back (`truncated: true`), never silent.
_MAX_INPUT_CHARS_PER_DOC: Final[int] = 30_000
# Fixed low sampling for reproducible extraction (NOT the chat temperature).
_COMPARE_TEMPERATURE: Final[float] = 0.0
# Room for a decomposed claim list / a table of contradictions with quotes.
_COMPARE_MAX_TOKENS: Final[int] = 4096
# Bound the concurrency of the decompose batch (two docs today, but explicit).
_DECOMPOSE_CONCURRENCY: Final[int] = 2


class _AtomicClaim(BaseModel):
    """One atomic, self-contained claim plus the exact text that supports it."""

    claim: str = Field(description="A single, self-contained factual assertion.")
    quote: str = Field(description="The exact sentence/phrase from the document supporting it.")


class _ClaimSet(BaseModel):
    """Structured-output container for one document's decomposition."""

    claims: list[_AtomicClaim] = Field(default_factory=list)


_DECOMPOSE_SYSTEM: Final[str] = (
    "You are a meticulous document analyst. Break the DOCUMENT into a list of "
    "atomic, self-contained claims. Work ONLY from what the document explicitly "
    "states — never infer, guess, or add outside knowledge. Include only "
    "SUBSTANTIVE, objective, verifiable claims (dates, numbers, amounts, "
    "responsibilities, sources/issuers, procedures, scope, policies, defined "
    "terms) — skip boilerplate, headings, and filler. Each claim must be ONE "
    "short sentence and carry the EXACT quote from the document that supports "
    "it (verbatim, not paraphrased). If the document states nothing "
    "substantive, return an empty list."
)

_VERIFY_SYSTEM: Final[str] = (
    "You are a meticulous analyst detecting CONTRADICTIONS between two "
    "documents, A and B. You are given the atomic claims extracted from each "
    "(with the exact source quote for each claim). Find every pair of claims — "
    "one from A, one from B — that genuinely CONTRADICT each other: statements "
    "that cannot both be true. Work ONLY from the quoted text; never infer "
    "beyond it, and do NOT report mere differences in wording, additional "
    "detail, or topics covered by only one document. Two documents that simply "
    "say different things are not contradicting.\n\n"
    "Classify each contradiction into EXACTLY ONE type:\n"
    "- temporal: conflicting dates or times of an event "
    '(e.g. "Starts Jan 15" vs "Starts end of Q1").\n'
    "- numerical: conflicting numbers, values, amounts, or percentages "
    '(e.g. "$12M surplus" vs "$5M deficit").\n'
    "- authority: a different source or issuer of a statement "
    '(e.g. "Issued by Compliance Office" vs "Issued by Strategy Unit").\n'
    "- process: conflicting procedures or operational routes "
    '(e.g. "Submit via HR portal" vs "Submit through admins").\n'
    "- policy_reversal: one statement directly negates the other "
    '(e.g. "Remote work mandatory" vs "Remote work not permitted").\n'
    "- specificity: one statement is broader or narrower than the other "
    '(e.g. "Applies globally" vs "Applies only to APAC").\n\n'
    "For each contradiction give: the type, a one-sentence description of the "
    "conflict, the EXACT conflicting quote from document A (quote_a), and the "
    "EXACT conflicting quote from document B (quote_b). If there are no genuine "
    "contradictions, return an empty list."
)


def _build_chat_model(model_id: str, region: str, max_tokens: int) -> Any:
    """Construct the pinned ``ChatBedrockConverse`` (the test seam).

    Imported lazily so importing this module (and the app) never requires the
    full LangChain import graph at process start, mirroring the guardrail pod's
    lazy NeMo import. Building the client performs NO network I/O — it binds a
    boto3 client on the ambient credential chain; the paid call happens only on
    ``invoke``/``batch``. Tests monkeypatch THIS function to inject a fake model.
    """
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(
        model=model_id,
        region_name=region,
        # Every pinned generator is an Anthropic model on Bedrock; setting it
        # explicitly avoids provider-inference edge cases with the `au.` prefix.
        provider="anthropic",
        temperature=_COMPARE_TEMPERATURE,
        max_tokens=max_tokens,
    )


def _decompose_messages(system: str, label: str, text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"DOCUMENT ({label}):\n{text}"},
    ]


def _render_claims(label: str, claim_set: _ClaimSet) -> str:
    """Render one document's claims as a numbered, quote-carrying block."""
    if not claim_set.claims:
        return f"Document {label}: (no substantive claims extracted)"
    lines = [f"Document {label} claims:"]
    for i, claim in enumerate(claim_set.claims, start=1):
        lines.append(f'{i}. {claim.claim.strip()}  [quote: "{claim.quote.strip()}"]')
    return "\n".join(lines)


def _verify_messages(
    label_a: str, claims_a: _ClaimSet, label_b: str, claims_b: _ClaimSet
) -> list[dict[str, str]]:
    body = (
        f"{_render_claims('A — ' + label_a, claims_a)}\n\n"
        f"{_render_claims('B — ' + label_b, claims_b)}"
    )
    return [
        {"role": "system", "content": _VERIFY_SYSTEM},
        {"role": "user", "content": body},
    ]


def _decompose(model: Any, request: CompareRequest, text_a: str, text_b: str) -> list[_ClaimSet]:
    """Stage 1: decompose both documents concurrently, resilient PER DOCUMENT.

    ``return_exceptions=True`` isolates the two documents: a PARSE failure on one
    degrades only THAT document to an empty claim set, so the other's claims are
    never discarded. An infra/unexpected fault on either item still propagates
    (it must not be swallowed into a fabricated "no contradictions"), matching
    :func:`_is_parse_error`'s contract.
    """
    decomposer = model.with_structured_output(_ClaimSet)
    batch = [
        _decompose_messages(_DECOMPOSE_SYSTEM, request.label_a, text_a),
        _decompose_messages(_DECOMPOSE_SYSTEM, request.label_b, text_b),
    ]
    results = decomposer.batch(
        batch, config={"max_concurrency": _DECOMPOSE_CONCURRENCY}, return_exceptions=True
    )
    out: list[_ClaimSet] = []
    for r in results:
        if isinstance(r, _ClaimSet):
            out.append(r)
        elif isinstance(r, BaseException) and not _is_parse_error(r):
            raise r
        else:
            out.append(_ClaimSet())
    return out


def _verify(
    model: Any, request: CompareRequest, claims_a: _ClaimSet, claims_b: _ClaimSet
) -> list[Contradiction]:
    """Stage 2: cross-examine the two claim lists; degrade to empty on failure."""
    verifier = model.with_structured_output(ContradictionReport)
    try:
        report = verifier.invoke(
            _verify_messages(request.label_a, claims_a, request.label_b, claims_b)
        )
    except Exception as exc:
        if _is_parse_error(exc):
            return []
        raise
    if not isinstance(report, ContradictionReport):
        return []
    return report.contradictions


def _is_parse_error(exc: Exception) -> bool:
    """True only for a model reply that could not be parsed into structured output.

    We degrade ONLY these to an empty result — the model ran but returned no
    valid tool call / a schema-invalid object. A genuine dependency fault (a
    Bedrock ``ClientError`` -> 502, a botocore network fault, or any unexpected
    bug) must NOT be swallowed: swallowing it would fabricate a "no
    contradictions" clean bill of health for a call that never actually ran, so
    those propagate and surface honestly. Imported lazily to keep importing this
    module free of the LangChain graph (mirrors ``_build_chat_model``).
    """
    from langchain_core.exceptions import OutputParserException
    from pydantic import ValidationError

    return isinstance(exc, (OutputParserException, ValidationError))


def _dedupe(contradictions: list[Contradiction]) -> list[Contradiction]:
    """Drop empty/degenerate entries and exact-duplicate conflicts, order-stable.

    A genuine contradiction has a description and the conflicting quote from
    BOTH documents; entries missing any of these are dropped rather than shown
    as blank table rows.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[Contradiction] = []
    for c in contradictions:
        desc, qa, qb = c.description.strip(), c.quote_a.strip(), c.quote_b.strip()
        if not (desc and qa and qb):
            continue
        key = (c.type, qa.lower(), qb.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Contradiction(type=c.type, description=desc, quote_a=qa, quote_b=qb)
        )
    return out


@compare_router.post("/compare", response_model=CompareResponse)
def post_compare(request: CompareRequest, http_request: Request) -> CompareResponse:
    """Detect contradictions between two documents (non-RAG, decompose-then-verify).

    Pins the SAME generator model + region as ``/query`` via ``pipeline_config``,
    decomposes each document into atomic quoted claims, then cross-examines the
    two claim sets into a typed, quote-backed contradiction list.
    """
    resolved: ResolvedPipelineConfig = resolve_pipeline_config(request.pipeline_config)
    generator = resolved.config.generator

    truncated = (
        len(request.document_a) > _MAX_INPUT_CHARS_PER_DOC
        or len(request.document_b) > _MAX_INPUT_CHARS_PER_DOC
    )
    text_a = request.document_a[:_MAX_INPUT_CHARS_PER_DOC]
    text_b = request.document_b[:_MAX_INPUT_CHARS_PER_DOC]

    model = _build_chat_model(generator.model_id, resolved.config.region, _COMPARE_MAX_TOKENS)

    claims_a, claims_b = _decompose(model, request, text_a, text_b)
    # No substantive claims on EITHER side -> nothing can contradict; skip the
    # verify call entirely and answer honestly.
    if not claims_a.claims and not claims_b.claims:
        contradictions: list[Contradiction] = []
    else:
        contradictions = _dedupe(_verify(model, request, claims_a, claims_b))

    return CompareResponse(
        contradictions=contradictions,
        model_id=generator.model_id,
        truncated=truncated,
    )
