"""
``POST /query`` + ``POST /query/stream``: retrieve → assemble → prompt → generate → cite.

Phase 2 (walking skeleton): the requested ``{name}-{semver}`` config is
resolved for real and the full stage chain runs against the lifespan-built
clients pulled from ``app.state`` (stages are typed pure functions that
RECEIVE client instances; they never import infra libraries). Every response
is ``generation_mode: "live"`` — the Phase 1 canned path is deleted and no
env flag or config ref can bring it back (``stub`` survives only as an
enum value in the envelope).

Phase 3 (system-prompt-leakage input guard): ``check_input`` is the FIRST
stage in the pre-generation chain and is now config-gated. Under a config that
enables the guard (``legal-rag-default-1.3.0``), a prompt-leak/injection
attempt raises :class:`app.orchestrator.guardrails.GuardrailTripwire` BEFORE
any paid call. That is a SUCCESSFUL, honest refusal — NOT an error: both
routes catch it specifically and return a 200 canned refusal envelope (answer
text = ``REFUSAL_TEXT``, empty citations, one ``block`` decision in
``guardrail_decisions[]``), never a 5xx. The Haiku guard call itself is a
``guardrail_input`` LLM span (model id + token counts + latency) recorded in
``_run_pre_generation`` on BOTH a SAFE allow and a block. On
``/query/stream`` the tripwire raises BEFORE ``StreamingResponse`` is built, so
exactly one ``final`` event and ZERO ``token`` events are emitted — nothing
leaks. Configs that leave the guard OFF (1.0.0/1.1.0/1.2.0, the eval lane)
behave byte-for-byte as before: ``check_input`` is a typed identity.

Phase 3 (deterministic output PII/secrets guard): after generation both routes
run ``check_output(generation.text, pins=...)`` — a PURE regex scan gated by
the config's ``output_categories``. A ``secrets`` hit raises ``GuardrailTripwire``
(reusing the SAME 200 canned-refusal path as the input block — NEVER a 5xx);
redactable PII is masked in place (a ``transform`` decision), advisory email/
phone is flagged (a ``flag`` decision), and citations are built over the
possibly-REDACTED text. On ``/query/stream`` a NON-EMPTY ``output_categories``
switches to BUFFERED delivery: ``token`` events are suppressed, the full answer
is accumulated and scanned, and exactly ONE ``final`` event carries the refusal
(block) or the possibly-redacted answer + its decisions (allow/redact) — the
only form that guarantees no secret byte reaches the user. An EMPTY
``output_categories`` (eval/1.1.0/1.2.0/1.3.0) keeps live token streaming
byte-for-byte.

Error mapping lives in app-level exception handlers registered in
``app.main`` — never per-route try/except:
- ``MalformedConfigRefError`` → 422, ``UnknownConfigError`` → 404,
  ``ConfigIntegrityError`` → 500 (Phase 1, unchanged)
- ``BedrockThrottleExhaustedError`` → 503 + ``Retry-After``
  (dependency: bedrock)
- OpenSearch failures / non-throttle Bedrock ``ClientError`` → 502 with the
  machine-readable ``dependency`` field
(``GuardrailTripwire`` is deliberately NOT in that taxonomy — it is a 200, not
a dependency failure.)

``POST /query/stream`` raises every PRE-generation failure through those same
handlers BEFORE the streaming response begins; once the 200 is committed,
failures become exactly one typed SSE ``error`` event (the generator's own
error-event emission is the ONLY per-route error handling anywhere here).

Phase B (multi-turn memory): BOTH routes accept the optional bounded
``history`` field (``app.schemas.query.HistoryTurn``). It feeds ONLY the
deterministic history-aware query rewrite and the prompt history block —
windows/char budgets come from the pinned config's ``history`` block — and is
NEVER echoed anywhere in ``result``, so the ``rag_query_output`` v1.1.0
contract and eval's ``orchestrator_query.py`` consumer are UNCHANGED (spec:
``agent-os/specs/2026-07-13-chainlit-chat-ui``). The rewritten retrieval
query is observable in the retrieval span's ``INPUT_VALUE`` — the honest
provenance channel for what was actually retrieved against.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from opensearchpy.exceptions import OpenSearchException

from app.clients.errors import BedrockThrottleExhaustedError, OpenSearchNotReadyError
from app.config import ResolvedPipelineConfig, Settings, resolve_pipeline_config
from app.observability import (
    citation_build_span,
    context_assembly_span,
    embedding_span,
    extract_trace_context,
    generation_span,
    record_span_error,
    resolve_tracer,
    retrieval_span,
    root_query_span,
    set_generation_attributes,
    set_guardrail_input_attributes,
    set_guardrail_output_attributes,
    set_retrieval_documents,
    span_context,
    start_citation_build_span,
    start_generation_span,
    start_guardrail_input_span,
    start_guardrail_output_span,
    start_root_query_span,
    trace_echo,
    use_context,
)
from app.orchestrator import guardrails, policy_router, query_rewrite, reranker
from app.orchestrator.citation_builder import build_citations
from app.orchestrator.context_assembler import assemble_context
from app.orchestrator.prompt_builder import build_prompt
from app.orchestrator.retriever import embed_query, retrieve
from app.schemas.envelope import QueryResponse
from app.schemas.errors import (
    DependencyErrorResponse,
    InternalErrorResponse,
    NotFoundErrorResponse,
    ValidationErrorResponse,
)
from app.schemas.query import QueryRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.trace import Span

    from app.clients import AppClients
    from app.clients.bedrock import GenerationResult
    from app.schemas.envelope import GuardrailDecision
    from app.schemas.pipeline_config import GuardrailsPin

query_router = APIRouter(tags=["query"])


def endpoint_host(endpoint: str | None) -> str | None:
    """Bare host from an endpoint URL (the Q8 provenance echo), or None."""
    if not endpoint:
        return None
    stripped = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    return stripped.split("/")[0].split(":")[0]


def _queried_index_scope(request: QueryRequest, settings: Settings) -> str:
    """
    The index scope actually queried for this request (provenance echo).

    Present ``retrieval_indices`` (project-scoped chat) ⇒ the comma-joined
    scope that reached OpenSearch's ``index=`` param, so replay stays
    attributable to exactly what was searched. ABSENT ⇒ the single Settings
    ``opensearch_index`` (legal-rag-bench) verbatim — byte-identical to today,
    so eval's rag_query_output v1.1.0 and the demo_ui are unaffected.
    """
    if request.retrieval_indices:
        return ",".join(request.retrieval_indices)
    return settings.opensearch_index


def _output_guard_active(pins: GuardrailsPin) -> bool:
    """
    Whether the deterministic output guard will actually scan for this config.

    True iff ``output_categories`` is non-empty — the exact condition under which
    ``check_output`` does real work (rather than returning a typed identity). The
    output guard is pure regex with no model dependency, so this is INDEPENDENT of
    ``enabled`` / the input classifier (matching the pure stage's own gate). The
    released ``1.0.0``–``1.3.0`` configs (empty ``output_categories``) add NO
    output-guard span and NO ``guardrail_output`` timing and stay byte-for-byte.
    """
    return bool(pins.output_categories)


def _sum_rule_counts(rationale: str | None) -> int | None:
    """
    Sum the per-rule counts embedded in an output-guard decision rationale.

    ``check_output`` builds a rationale like ``"ssn=2, credit_card=1"`` for a
    ``transform``/``flag`` decision; this recovers the total span count (``3``)
    for the ``guardrail.count`` span attribute. Returns ``None`` when the
    rationale carries no ``label=N`` parts (e.g. a secrets block, whose rationale
    names the secret class, not a count).
    """
    if not rationale:
        return None
    total = 0
    found = False
    for part in rationale.split(","):
        _, sep, number = part.partition("=")
        number = number.strip()
        if sep and number.isdigit():
            total += int(number)
            found = True
    return total if found else None


def _record_output_guard_decisions(span: Span, decisions: tuple[GuardrailDecision, ...]) -> None:
    """
    Attach the (allow/transform/flag) output-guard outcome to its span.

    A clean scan records ``decision="allow"`` with no category/rule/count; a
    non-empty scan records the PRIMARY decision (redaction ranks above an
    advisory flag, so ``decisions[0]`` — the ``transform`` when present) and a
    ``count`` summed across ALL decisions (redactions + advisory flags), so a
    turn that both masks and flags reports the full item total on the span. A
    secrets BLOCK never reaches here (it raises ``GuardrailTripwire`` and is
    recorded on the tripwire path instead).
    """
    if not decisions:
        set_guardrail_output_attributes(span, decision="allow")
        return
    primary = decisions[0]
    total = sum((_sum_rule_counts(d.rationale) or 0) for d in decisions) or None
    set_guardrail_output_attributes(
        span,
        decision=primary.decision,
        category=primary.category,
        rule_id=primary.rule_id,
        count=total,
    )


def _nemo_output_active(pins: GuardrailsPin) -> bool:
    """
    Whether the out-of-process NeMo output/facts guard runs for this config.

    True iff the config carries an enabled ``nemo`` selector with output
    self-check on (``guardrails.nemo_output_active``). The released
    ``1.0.0``-``1.4.0`` configs carry no ``nemo`` selector, so this is False and
    the pod is never called — those lanes stay byte-for-byte. Like
    ``_output_guard_active`` this drives buffered streaming + span emission.
    """
    return guardrails.nemo_output_active(pins)


def _chunk_texts(chunks: list[dict[str, Any]]) -> list[str]:
    """Grounding evidence for the NeMo facts rail: the retrieved chunk texts."""
    return [str(chunk.get("text", "")) for chunk in chunks]


def _record_nemo_output_decisions(span: Span, result: Any) -> None:
    """
    Attach the NeMo output/facts outcome (allow | advisory flag) to its span.

    A clean pass records ``decision="allow"``; an advisory records the flag
    decision. BOTH carry the pod-stamped ``model_id`` + token counts so the NeMo
    guard call's model and cost render in Phoenix (unlike the regex output guard,
    which has no model). A NeMo BLOCK never reaches here — it raises
    ``GuardrailTripwire`` and is recorded on the tripwire path instead.
    """
    if not result.decisions:
        set_guardrail_output_attributes(
            span,
            decision="allow",
            model_id=result.model_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return
    primary = result.decisions[0]
    set_guardrail_output_attributes(
        span,
        decision=primary.decision,
        category=primary.category,
        rule_id=primary.rule_id,
        model_id=result.model_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def _run_nemo_output_guard(
    tracer: Any,
    guard_pins: GuardrailsPin,
    clients: AppClients,
    *,
    answer_text: str,
    chunks: list[dict[str, Any]],
    timings_ms: dict[str, float],
    context: Any | None = None,
) -> tuple[str, list[GuardrailDecision]]:
    """
    Run the NeMo output/facts guard over the (possibly-redacted) answer.

    Opens a ``guardrail_output`` span for the pod call (model id + token counts),
    calls the PURE ``check_output_nemo`` with the injected client, and returns the
    unchanged answer plus any advisory ``flag`` decisions. A NeMo BLOCK raises
    ``GuardrailTripwire`` (span recorded + ended before it propagates) for the
    route to turn into a 200 canned refusal — NeMo's string never becomes the
    answer (I3). A no-op returning ``(answer_text, [])`` when the lane is off.
    The output/facts fail-OPEN policy lives in the pure stage, so a flaky/
    unreachable pod returns an advisory flag here, never a block.
    """
    if not _nemo_output_active(guard_pins):
        return answer_text, []
    span = start_guardrail_output_span(tracer, context=context)
    start = time.perf_counter()
    try:
        result = guardrails.check_output_nemo(
            answer_text, _chunk_texts(chunks), pins=guard_pins, nemo_client=clients.nemo
        )
    except guardrails.GuardrailTripwire as tripwire:
        timings_ms["guardrail_nemo_output"] = (time.perf_counter() - start) * 1000.0
        set_guardrail_output_attributes(
            span,
            decision=tripwire.decision.decision,
            category=tripwire.decision.category,
            rule_id=tripwire.decision.rule_id,
            model_id=tripwire.model_id,
            input_tokens=tripwire.input_tokens,
            output_tokens=tripwire.output_tokens,
        )
        span.end()
        raise
    timings_ms["guardrail_nemo_output"] = (time.perf_counter() - start) * 1000.0
    _record_nemo_output_decisions(span, result)
    span.end()
    return result.answer_text, list(result.decisions)


@dataclass(frozen=True)
class PreGenerationResult:
    """Outputs of the shared pre-generation chain both delivery paths reuse."""

    question: str  # post-rewrite question (what retrieval actually ran on)
    chunks: list[dict[str, Any]]
    prompt: str


def _run_pre_generation(
    request: QueryRequest,
    resolved: ResolvedPipelineConfig,
    settings: Settings,
    clients: AppClients,
    tracer: Any,
    timings_ms: dict[str, float],
) -> PreGenerationResult:
    """
    Run the ONE pre-generation chain both ``/query`` and ``/query/stream`` call.

    guardrails → policy_router → query_rewrite → embed → retrieve → rerank →
    assemble → prompt, with ``_timed`` timings and the OpenInference span
    helpers exactly as the blocking path always recorded them. Must be called
    with the root query span CURRENT so stage spans parent correctly.

    ``guardrails.check_input`` is FIRST and is config-gated: it receives the
    resolved ``guardrails`` pins and the injected classifier. When the guard is
    disabled (1.0.0/1.1.0/1.2.0) it is a typed identity — no behavior change and
    no classifier call. When enabled and a system-prompt-leakage attempt is
    detected it raises ``GuardrailTripwire``, which PROPAGATES here unchanged
    (no try/except in the pure chain) for the routes to turn into a 200 refusal.

    ``request.history`` + the resolved config's history pins feed ONLY
    ``query_rewrite`` (the retrieval query — the history-prefixed rewrite is
    what embedding/retrieval run on, echoed in the retrieval span's
    ``INPUT_VALUE``) and ``build_prompt`` (the ``{history}`` block; the
    prompt's ``{question}`` stays the CURRENT question). No other stage
    touches history and nothing echoes it into ``result``.

    Failures raise UNCHANGED into the app-level exception handlers — no
    try/except here (per-route error mapping is forbidden by design).
    """
    # Config-gated input guard, then the parked policy-router identity — both
    # stay in-chain at their natural first positions.
    guard_pins = resolved.config.guardrails
    if guardrails.prefilter_hit(request.question, guard_pins) and clients.classifier is not None:
        # The Haiku classifier WILL run: wrap it in an LLM span so the guard call
        # is visible in Phoenix (model id, token counts, latency) on a SAFE allow
        # AND a block — parents to the current root span (both routes). A block
        # raises through here; we record the outcome and end the span CLEANLY
        # (not an error) before re-raising for the route to turn into a refusal.
        guard_span = start_guardrail_input_span(tracer, model_id=guard_pins.classifier_model_id)
        try:
            guard_result = guardrails.check_input(
                request.question, pins=guard_pins, classifier=clients.classifier
            )
        except guardrails.GuardrailTripwire as tripwire:
            set_guardrail_input_attributes(
                guard_span,
                decision=tripwire.decision.decision,
                category=tripwire.decision.category,
                rule_id=tripwire.decision.rule_id,
                input_tokens=tripwire.input_tokens,
                output_tokens=tripwire.output_tokens,
            )
            guard_span.end()
            raise
        set_guardrail_input_attributes(
            guard_span,
            decision="allow",
            input_tokens=guard_result.input_tokens,
            output_tokens=guard_result.output_tokens,
        )
        guard_span.end()
        question = guard_result.question
    else:
        # Gate off, pre-filter miss, or misconfiguration (classifier None) → no
        # classifier call and no span. Misconfiguration still raises loudly here.
        question = guardrails.check_input(
            request.question, pins=guard_pins, classifier=clients.classifier
        ).question

    # Out-of-process NeMo INPUT self-check lane (the nemo-all 1.8.0 config). The
    # orchestrator regex pre-filter is the FREE cost gate: a benign pre-filter
    # MISS makes ZERO paid pod calls and adds no span; a HIT forwards the RAW
    # question (unchanged, NOT normalized) to the pod's self_check_input, whose
    # LLM verdict REPLACES the in-house Haiku confirm-step on this config. A block
    # raises GuardrailTripwire, which PROPAGATES here unchanged (no try/except in
    # the pure chain) for the routes to turn into a 200 refusal — never a 5xx
    # (I3). Off (in-house 1.0.0-1.4.0, no input_self_check) ⇒ skipped entirely.
    if guardrails.nemo_prefilter_hit(request.question, guard_pins):
        nemo_in_span = start_guardrail_input_span(tracer)
        try:
            nemo_in = guardrails.check_input_nemo(
                request.question, pins=guard_pins, nemo_client=clients.nemo
            )
        except guardrails.GuardrailTripwire as tripwire:
            set_guardrail_input_attributes(
                nemo_in_span,
                decision=tripwire.decision.decision,
                category=tripwire.decision.category,
                rule_id=tripwire.decision.rule_id,
                model_id=tripwire.model_id,
                input_tokens=tripwire.input_tokens,
                output_tokens=tripwire.output_tokens,
            )
            nemo_in_span.end()
            raise
        set_guardrail_input_attributes(
            nemo_in_span,
            decision="allow",
            model_id=nemo_in.model_id,
            input_tokens=nemo_in.input_tokens,
            output_tokens=nemo_in.output_tokens,
        )
        nemo_in_span.end()
        question = nemo_in.question

    question = policy_router.route(question)
    # Deterministic history-aware rewrite: builds the RETRIEVAL query only.
    # With history absent it is the identity, so single-turn behavior (and
    # the Phase A golden bytes) are unchanged.
    retrieval_query = query_rewrite.rewrite(question, request.history, resolved.config.history)

    with (
        _timed(timings_ms, "embedding"),
        embedding_span(tracer, model_id=resolved.config.embedder.model_id),
    ):
        query_vector = embed_query(retrieval_query, resolved.config, embedder=clients.bedrock)

    with (
        _timed(timings_ms, "retrieval"),
        retrieval_span(
            tracer,
            question=retrieval_query,
            index=_queried_index_scope(request, settings),
            host=endpoint_host(settings.opensearch_endpoint),
        ) as retrieval,
    ):
        chunks = retrieve(
            retrieval_query,
            query_vector,
            resolved.config,
            settings,
            search_client=clients.search,
            indices=request.retrieval_indices,
        )
        set_retrieval_documents(retrieval, chunks)

    chunks = reranker.rerank(chunks)

    with (
        _timed(timings_ms, "context_assembly"),
        context_assembly_span(tracer, chunks_in=len(chunks)) as assembly,
    ):
        context = assemble_context(chunks, char_budget=resolved.config.context.char_budget)
        assembly.set_attribute("context.char_count", len(context))

    prompt = build_prompt(
        resolved.config.prompt_template.text,
        context=context,
        question=question,
        history=request.history,
        pins=resolved.config.history,
    )

    return PreGenerationResult(question=retrieval_query, chunks=chunks, prompt=prompt)


def _build_result(
    request: QueryRequest,
    resolved: ResolvedPipelineConfig,
    settings: Settings,
    *,
    retrieved_chunks: list[dict[str, Any]],
    answer_text: str,
    citations: list[dict[str, Any]],
    timings_ms: dict[str, float],
    trace_block: dict[str, str] | None,
) -> dict[str, Any]:
    """
    Build the ``result`` payload (eval rag_query_output v1.1.0) from live data.

    ``system_version`` carries the config provenance (``pipeline_version`` +
    ``config_sha256``), the model pins, AND the Q8 provenance echo (resolved
    index name + endpoint host) — ``system_version`` is the schema's
    deliberately-open echo slot. ``trace`` is a declared optional slot,
    included only when tracing is real (OMITTED, never null, otherwise);
    ``timings_ms`` is always present.

    A JSON ``"metadata": null`` is treated as absent (the key is omitted from
    the echo) because the eval schema requires ``"type": "object"`` for
    ``query.metadata``.
    """
    query: dict[str, Any] = {
        # query_id is required by the schema; generate one ONLY when the field
        # is absent — any provided value (even "") is echoed verbatim.
        "query_id": request.query_id if request.query_id is not None else str(uuid.uuid4()),
        "text": request.question,
    }
    if request.metadata is not None:
        # Echoed VERBATIM; never read by any pipeline stage (metadata-invariant).
        query["metadata"] = request.metadata

    system_version: dict[str, Any] = {
        "pipeline_version": resolved.pipeline_version,
        "config_sha256": resolved.config_sha256,
        "generator_model": resolved.config.generator.model_id,
        "embedder_model": resolved.config.embedder.model_id,
        # Q8 provenance echo: identical config_sha256 against a DIFFERENT
        # index must stay attributable in replay comparisons. Echoes the
        # ACTUALLY-queried scope when retrieval_indices is present, else the
        # Settings default (absent-field byte-identical to today).
        "opensearch_index": _queried_index_scope(request, settings),
    }
    host = endpoint_host(settings.opensearch_endpoint)
    if host is not None:
        system_version["opensearch_host"] = host

    result: dict[str, Any] = {
        "schema_version": "1.1.0",
        "system_version": system_version,
        "query": query,
        "answer": {"text": answer_text, "citations": citations},
        "retrieved_chunks": retrieved_chunks,
        "timings_ms": timings_ms,
    }
    if trace_block is not None:
        result["trace"] = trace_block
    return result


def _blocked_result(
    request: QueryRequest,
    resolved: ResolvedPipelineConfig,
    settings: Settings,
    *,
    timings_ms: dict[str, float],
    trace_block: dict[str, str] | None,
) -> dict[str, Any]:
    """
    Build the schema-valid refusal ``result`` for a guard block.

    A refusal is just an answer with the canned ``REFUSAL_TEXT``, an EMPTY
    ``citations`` array, and an EMPTY ``retrieved_chunks`` array. For the INPUT
    guard the guard short-circuits BEFORE retrieval, so ``retrieved_chunks``
    stays ``[]``; the OUTPUT secrets block happens AFTER retrieval but still
    suppresses the whole answer to the canned refusal, and by contract a refusal
    carries no citations and no retrieved chunks (the answer that referenced them
    is gone). ``system_version`` still echoes the config provenance honestly —
    the pinned config that decided to block is on record — so this stays within
    the same ``rag_query_output`` v1.1.0 contract.
    """
    return _build_result(
        request,
        resolved,
        settings,
        retrieved_chunks=[],
        answer_text=guardrails.REFUSAL_TEXT,
        citations=[],
        timings_ms=timings_ms,
        trace_block=trace_block,
    )


@query_router.post(
    "/query",
    response_model=QueryResponse,
    responses={
        404: {
            "model": NotFoundErrorResponse,
            "description": "Unknown (but well-formed) pipeline_config reference.",
        },
        422: {
            "model": ValidationErrorResponse,
            "description": "Request validation failed (e.g. malformed pipeline_config ref).",
        },
        500: {
            "model": InternalErrorResponse,
            "description": "Packaged config artifact failed integrity verification.",
        },
        502: {
            "model": DependencyErrorResponse,
            "description": (
                "An upstream dependency failed: OpenSearch search failure, or "
                "a non-throttle Bedrock error. The body names the dependency."
            ),
        },
        503: {
            "model": DependencyErrorResponse,
            "description": (
                "Bedrock throttling persisted past the bounded retry budget. "
                "Transient — retry after the Retry-After interval."
            ),
            "headers": {
                "Retry-After": {
                    "description": "Seconds to wait before retrying the query.",
                    "schema": {"type": "string"},
                }
            },
        },
    },
)
def post_query(request: QueryRequest, http_request: Request) -> QueryResponse:
    """
    Answer a single-turn RAG query with the LIVE pipeline.

    Config resolution is REAL (pins echoed in ``result.system_version``,
    incl. ``config_sha256``); the chain is policy_router → query_rewrite →
    embed → retrieve → rerank → assemble → prompt → generate → guardrails →
    cite, with clients pulled from ``app.state`` and spans/timings recorded
    OUTSIDE the pure stages. The ``result`` block validates verbatim against
    eval's rag_query_output v1.1.0.
    """
    resolved = resolve_pipeline_config(request.pipeline_config)
    state = http_request.app.state
    settings: Settings = state.settings
    clients: AppClients = state.clients
    tracer = resolve_tracer(state)

    if clients.search is None:
        # OpenSearch client never came up (endpoint unset, _meta mismatch, or
        # unreachable at startup) → the app-level handler maps this to 502.
        raise OpenSearchNotReadyError(
            clients.opensearch_unavailable_reason or "OpenSearch client is unavailable."
        )

    timings_ms: dict[str, float] = {}
    total_start = time.perf_counter()
    with root_query_span(
        tracer,
        question=request.question,
        context=extract_trace_context(http_request.headers),
    ) as root_span:
        try:
            pre = _run_pre_generation(request, resolved, settings, clients, tracer, timings_ms)
        except guardrails.GuardrailTripwire as tripwire:
            # A successful, honest refusal — NOT an error. The guardrail_input
            # LLM span (with the block outcome) was already recorded and ended
            # inside _run_pre_generation; here we just build the canned refusal
            # envelope and return 200 with the block decision attached.
            # perf_counter is called ONLY on this tripwire path so the allowed
            # path's deterministic-clock ticks (the golden bytes) are unchanged.
            timings_ms["guardrail"] = (time.perf_counter() - total_start) * 1000.0
            trace_block = trace_echo(root_span)
            timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0
            result = _blocked_result(
                request, resolved, settings, timings_ms=timings_ms, trace_block=trace_block
            )
            return QueryResponse(
                result=result,
                guardrail_decisions=[tripwire.decision],
                generation_mode="live",
            )

        generator_pin = resolved.config.generator
        with (
            _timed(timings_ms, "generation"),
            generation_span(tracer, model_id=generator_pin.model_id, prompt=pre.prompt) as gen_span,
        ):
            generation: GenerationResult = clients.bedrock.generate(
                pre.prompt,
                model_id=generator_pin.model_id,
                temperature=generator_pin.temperature,
                max_tokens=generator_pin.max_tokens,
            )
            set_generation_attributes(gen_span, generation)

        # Config-gated deterministic output guard (PURE regex, NO model call).
        # An empty output_categories (1.0.0/1.1.0/1.2.0/1.3.0) makes check_output
        # a typed identity: NO span, NO guardrail_output timing → byte-for-byte.
        # A secrets hit raises GuardrailTripwire and reuses the SAME 200 refusal
        # path as the input block; redactable PII is masked and email/phone
        # flagged, and citations are built over the possibly-REDACTED text.
        guard_pins = resolved.config.guardrails
        if _output_guard_active(guard_pins):
            guard_out_span = start_guardrail_output_span(tracer)
            out_guard_start = time.perf_counter()
            # try/finally guarantees the span is ended on EVERY path (normal,
            # the tripwire's early return, or an unexpected attribute-set error).
            try:
                out = guardrails.check_output(generation.text, pins=guard_pins)
                timings_ms["guardrail_output"] = (time.perf_counter() - out_guard_start) * 1000.0
                _record_output_guard_decisions(guard_out_span, out.decisions)
                answer_text = out.answer_text
                guardrail_decisions = list(out.decisions)
            except guardrails.GuardrailTripwire as tripwire:
                timings_ms["guardrail_output"] = (time.perf_counter() - out_guard_start) * 1000.0
                set_guardrail_output_attributes(
                    guard_out_span,
                    decision=tripwire.decision.decision,
                    category=tripwire.decision.category,
                    rule_id=tripwire.decision.rule_id,
                )
                trace_block = trace_echo(root_span)
                timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0
                result = _blocked_result(
                    request, resolved, settings, timings_ms=timings_ms, trace_block=trace_block
                )
                return QueryResponse(
                    result=result,
                    guardrail_decisions=[tripwire.decision],
                    generation_mode="live",
                )
            finally:
                guard_out_span.end()
        else:
            answer_text = generation.text
            guardrail_decisions = []

        # Out-of-process NeMo output/facts guard (Q6b: OUTPUT + FACTS only). Runs
        # over the possibly-REDACTED answer and the retrieved chunks (facts
        # grounding). A genuine NeMo block raises GuardrailTripwire and reuses the
        # SAME 200 canned-refusal path as the regex secrets block (I3) — NeMo's
        # own string NEVER becomes the answer; an advisory flag (or a fail-OPEN
        # unreachable pod, Q2) appends a non-block decision and delivers the
        # answer. Off (1.0.0-1.4.0, no nemo selector) ⇒ a no-op.
        try:
            answer_text, nemo_decisions = _run_nemo_output_guard(
                tracer,
                guard_pins,
                clients,
                answer_text=answer_text,
                chunks=pre.chunks,
                timings_ms=timings_ms,
            )
            guardrail_decisions.extend(nemo_decisions)
        except guardrails.GuardrailTripwire as tripwire:
            trace_block = trace_echo(root_span)
            timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0
            result = _blocked_result(
                request, resolved, settings, timings_ms=timings_ms, trace_block=trace_block
            )
            return QueryResponse(
                result=result,
                guardrail_decisions=[tripwire.decision],
                generation_mode="live",
            )

        with (
            _timed(timings_ms, "citation_build"),
            citation_build_span(tracer) as citation_span,
        ):
            citation_result = build_citations(
                answer_text, {chunk["chunk_id"] for chunk in pre.chunks}
            )
            citation_span.set_attribute("citation.count", len(citation_result.citations))
            citation_span.set_attribute(
                "citation.dropped_unknown_marker_count",
                citation_result.dropped_unknown_marker_count,
            )

        trace_block = trace_echo(root_span)
    timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0

    result = _build_result(
        request,
        resolved,
        settings,
        retrieved_chunks=pre.chunks,
        answer_text=answer_text,
        citations=citation_result.citations,
        timings_ms=timings_ms,
        trace_block=trace_block,
    )
    return QueryResponse(
        result=result, guardrail_decisions=guardrail_decisions, generation_mode="live"
    )


_SSE_EVENT_CONTRACT_DOC = (
    "Server-Sent Events stream (OpenAPI cannot express event streams; the "
    "contract is documented here). Events, in order:\n\n"
    '- `event: token` — `data: {"text": "<delta>"}` per Bedrock content '
    "delta, forwarded RAW as received (citation markers included, "
    "unresolved).\n"
    "- `event: final` — `data:` the full QueryResponse envelope, identical "
    "in shape to POST /query's 200 body (result validates against "
    "rag_query_output v1.1.0; citations built over the FULL accumulated "
    "text). Emitted exactly once.\n"
    '- `event: error` — `data: {"detail": str, "dependency"?: '
    '"opensearch"|"bedrock", "retry_after_seconds"?: int, '
    '"http_equivalent": int}`, mirroring the JSON error taxonomy, for '
    "failures AFTER the 200 is committed (e.g. a mid-stream throttle); the "
    "stream then closes.\n\n"
    "Every stream ends in EXACTLY ONE terminal event (`final` or `error`) — "
    "silent truncation is forbidden. Pre-generation failures are raised "
    "BEFORE the stream starts and return the plain HTTP errors documented "
    "below."
)


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """Render one SSE frame; JSON keeps the data line newline-free."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_error_payload(error: Exception) -> dict[str, Any]:
    """
    Map a post-commit failure to the typed SSE ``error`` payload.

    Mirrors the app-level JSON error taxonomy (``DependencyErrorResponse``
    fields plus ``http_equivalent`` — the status the failure would have
    mapped to had it happened before the 200 was committed). Raw exception
    text never leaks for AWS/OpenSearch errors, matching the handlers.
    """
    if isinstance(error, BedrockThrottleExhaustedError):
        # Lazy import: app.main imports this module at startup (no cycle at
        # request time); reuse its single Retry-After constant.
        from app.main import _RETRY_AFTER_SECONDS

        return {
            "detail": str(error),
            "dependency": "bedrock",
            "retry_after_seconds": _RETRY_AFTER_SECONDS,
            "http_equivalent": 503,
        }
    if isinstance(error, ClientError):
        response = getattr(error, "response", None)
        code = "unknown"
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code", "unknown")
        return {
            "detail": f"Bedrock request failed (AWS error code: {code}).",
            "dependency": "bedrock",
            "http_equivalent": 502,
        }
    if isinstance(error, OpenSearchException):
        return {
            "detail": f"OpenSearch search failed ({type(error).__name__}).",
            "dependency": "opensearch",
            "http_equivalent": 502,
        }
    return {
        "detail": f"Streaming failed ({type(error).__name__}).",
        "http_equivalent": 500,
    }


@query_router.post(
    "/query/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": _SSE_EVENT_CONTRACT_DOC,
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        404: {
            "model": NotFoundErrorResponse,
            "description": "Unknown (but well-formed) pipeline_config reference.",
        },
        422: {
            "model": ValidationErrorResponse,
            "description": "Request validation failed (e.g. malformed pipeline_config ref).",
        },
        500: {
            "model": InternalErrorResponse,
            "description": "Packaged config artifact failed integrity verification.",
        },
        502: {
            "model": DependencyErrorResponse,
            "description": (
                "An upstream dependency failed BEFORE streaming began: "
                "OpenSearch failure, or a non-throttle Bedrock error during "
                "query embedding. The body names the dependency."
            ),
        },
        503: {
            "model": DependencyErrorResponse,
            "description": (
                "Bedrock throttling persisted past the bounded retry budget "
                "BEFORE streaming began. Transient — retry after the "
                "Retry-After interval."
            ),
            "headers": {
                "Retry-After": {
                    "description": "Seconds to wait before retrying the query.",
                    "schema": {"type": "string"},
                }
            },
        },
    },
)
def post_query_stream(request: QueryRequest, http_request: Request) -> StreamingResponse:
    """
    Answer a RAG query with the LIVE pipeline, streamed as SSE.

    Optionally multi-turn: the bounded request-only ``history`` field feeds
    the deterministic query rewrite and the prompt history block and is never
    echoed in the ``final`` envelope (rag_query_output v1.1.0 unchanged).
    Same request model and pre-generation chain as ``POST /query``; only
    delivery differs: generation streams from Bedrock and each text delta is
    forwarded raw as an SSE ``token`` event, then guardrails + citation build
    run over the FULL accumulated answer and the complete QueryResponse
    envelope is emitted as the single ``final`` event. Pre-generation
    failures raise BEFORE the stream starts (plain HTTP errors via the
    app-level handlers); post-commit failures become exactly one typed
    ``error`` event. See the 200 response description for the event contract.
    """
    resolved = resolve_pipeline_config(request.pipeline_config)
    state = http_request.app.state
    settings: Settings = state.settings
    clients: AppClients = state.clients
    tracer = resolve_tracer(state)

    if clients.search is None:
        # OpenSearch client never came up → the app-level handler maps this
        # to 502 (the same pre-flight guard as the blocking route).
        raise OpenSearchNotReadyError(
            clients.opensearch_unavailable_reason or "OpenSearch client is unavailable."
        )

    timings_ms: dict[str, float] = {}
    total_start = time.perf_counter()
    # The root span outlives this handler (the generator ends it), so it is
    # started explicitly — never made ambient-current here (thread hops).
    root_span = start_root_query_span(
        tracer,
        question=request.question,
        route="POST /query/stream",
        context=extract_trace_context(http_request.headers),
    )
    root_context = span_context(root_span)

    try:
        with use_context(root_context):
            pre = _run_pre_generation(request, resolved, settings, clients, tracer, timings_ms)
    except guardrails.GuardrailTripwire as tripwire:
        # Config-gated input-guard BLOCK raised BEFORE StreamingResponse is
        # constructed: a successful, honest refusal (NOT an error, distinct from
        # the record_span_error/re-raise path below). The guardrail_input LLM
        # span (block outcome) was already recorded inside _run_pre_generation
        # (parented via the ambient root context); here we end the root cleanly
        # and return a stream that emits EXACTLY one final refusal — no tokens.
        timings_ms["guardrail"] = (time.perf_counter() - total_start) * 1000.0
        trace_block = trace_echo(root_span)
        timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0
        root_span.end()
        result = _blocked_result(
            request, resolved, settings, timings_ms=timings_ms, trace_block=trace_block
        )
        envelope = QueryResponse(
            result=result,
            guardrail_decisions=[tripwire.decision],
            generation_mode="live",
        )

        def refusal_stream() -> Iterator[str]:
            """Exactly ONE final event (the canned refusal); ZERO token events."""
            yield _sse_event("final", envelope.model_dump(mode="json"))

        return StreamingResponse(refusal_stream(), media_type="text/event-stream")
    except BaseException as error:
        # Pre-stream failure: span bookkeeping only (close the root span),
        # then re-raise UNCHANGED into the app-level exception handlers.
        record_span_error(root_span, error)
        root_span.end()
        raise

    generator_pin = resolved.config.generator
    guard_pins = resolved.config.guardrails
    regex_output_active = _output_guard_active(guard_pins)
    nemo_output_active = _nemo_output_active(guard_pins)
    # BUFFERED delivery when ANY output guard is active: no token events reach
    # the client until the COMPLETE answer has cleared the output guard(s) — the
    # regex PII/secrets scan and/or the out-of-process NeMo output/facts rails.
    buffered = regex_output_active or nemo_output_active

    def event_stream() -> Iterator[str]:
        """
        token* then EXACTLY ONE terminal event (final | error).

        In BUFFERED mode zero token events are emitted; a secrets block or an
        allow/redact both terminate in exactly one ``final``.
        """
        open_spans: list[Span] = []
        try:
            gen_span = start_generation_span(
                tracer,
                model_id=generator_pin.model_id,
                prompt=pre.prompt,
                context=root_context,
            )
            open_spans.append(gen_span)
            with _timed(timings_ms, "generation"):
                stream = clients.bedrock.generate_stream(
                    pre.prompt,
                    model_id=generator_pin.model_id,
                    temperature=generator_pin.temperature,
                    max_tokens=generator_pin.max_tokens,
                )
                for delta in stream:
                    # Buffered mode SUPPRESSES token events (the answer may carry
                    # a secret prefix); live mode forwards each delta raw.
                    if not buffered:
                        yield _sse_event("token", {"text": delta})
                generation = stream.result()
                set_generation_attributes(gen_span, generation)
            gen_span.end()
            open_spans.remove(gen_span)

            # Config-gated deterministic output guard. In buffered mode a secrets
            # block terminates the stream in one final refusal (zero tokens);
            # redactable PII is masked and email/phone flagged, with citations
            # built over the possibly-REDACTED text. Live mode (empty
            # output_categories) runs check_output as a typed identity — NO span,
            # NO guardrail_output timing → byte-for-byte unchanged.
            if regex_output_active:
                guard_out_span = start_guardrail_output_span(tracer, context=root_context)
                open_spans.append(guard_out_span)
                out_guard_start = time.perf_counter()
                try:
                    out = guardrails.check_output(generation.text, pins=guard_pins)
                except guardrails.GuardrailTripwire as tripwire:
                    # A successful, honest refusal — NOT an error. Caught HERE
                    # (before the outer except) so it is never emitted as an
                    # error event: exactly one final refusal, zero tokens.
                    timings_ms["guardrail_output"] = (
                        time.perf_counter() - out_guard_start
                    ) * 1000.0
                    set_guardrail_output_attributes(
                        guard_out_span,
                        decision=tripwire.decision.decision,
                        category=tripwire.decision.category,
                        rule_id=tripwire.decision.rule_id,
                    )
                    guard_out_span.end()
                    open_spans.remove(guard_out_span)
                    trace_block = trace_echo(root_span)
                    timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0
                    result = _blocked_result(
                        request,
                        resolved,
                        settings,
                        timings_ms=timings_ms,
                        trace_block=trace_block,
                    )
                    envelope = QueryResponse(
                        result=result,
                        guardrail_decisions=[tripwire.decision],
                        generation_mode="live",
                    )
                    yield _sse_event("final", envelope.model_dump(mode="json"))
                    return
                timings_ms["guardrail_output"] = (time.perf_counter() - out_guard_start) * 1000.0
                _record_output_guard_decisions(guard_out_span, out.decisions)
                guard_out_span.end()
                open_spans.remove(guard_out_span)
                answer_text = out.answer_text
                guardrail_decisions = list(out.decisions)
            else:
                answer_text = generation.text
                guardrail_decisions = []

            # Out-of-process NeMo output/facts guard (buffered: no tokens have
            # shipped). A block terminates the stream in ONE final canned refusal
            # (I3 — NeMo's string never becomes the answer); an advisory flag (or
            # a fail-OPEN unreachable pod, Q2) appends a non-block decision and
            # delivers the answer. Off (1.0.0-1.4.0) ⇒ skipped.
            if nemo_output_active:
                nemo_span = start_guardrail_output_span(tracer, context=root_context)
                open_spans.append(nemo_span)
                nemo_start = time.perf_counter()
                try:
                    nemo_out = guardrails.check_output_nemo(
                        answer_text,
                        _chunk_texts(pre.chunks),
                        pins=guard_pins,
                        nemo_client=clients.nemo,
                    )
                except guardrails.GuardrailTripwire as tripwire:
                    # A successful, honest refusal — caught HERE (before the outer
                    # except) so it is never an error event: one final, zero tokens.
                    timings_ms["guardrail_nemo_output"] = (
                        time.perf_counter() - nemo_start
                    ) * 1000.0
                    set_guardrail_output_attributes(
                        nemo_span,
                        decision=tripwire.decision.decision,
                        category=tripwire.decision.category,
                        rule_id=tripwire.decision.rule_id,
                        model_id=tripwire.model_id,
                        input_tokens=tripwire.input_tokens,
                        output_tokens=tripwire.output_tokens,
                    )
                    nemo_span.end()
                    open_spans.remove(nemo_span)
                    trace_block = trace_echo(root_span)
                    timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0
                    result = _blocked_result(
                        request,
                        resolved,
                        settings,
                        timings_ms=timings_ms,
                        trace_block=trace_block,
                    )
                    envelope = QueryResponse(
                        result=result,
                        guardrail_decisions=[tripwire.decision],
                        generation_mode="live",
                    )
                    yield _sse_event("final", envelope.model_dump(mode="json"))
                    return
                timings_ms["guardrail_nemo_output"] = (
                    time.perf_counter() - nemo_start
                ) * 1000.0
                _record_nemo_output_decisions(nemo_span, nemo_out)
                nemo_span.end()
                open_spans.remove(nemo_span)
                answer_text = nemo_out.answer_text
                guardrail_decisions.extend(nemo_out.decisions)

            citation_span = start_citation_build_span(tracer, context=root_context)
            open_spans.append(citation_span)
            with _timed(timings_ms, "citation_build"):
                citation_result = build_citations(
                    answer_text, {chunk["chunk_id"] for chunk in pre.chunks}
                )
                citation_span.set_attribute("citation.count", len(citation_result.citations))
                citation_span.set_attribute(
                    "citation.dropped_unknown_marker_count",
                    citation_result.dropped_unknown_marker_count,
                )
            citation_span.end()
            open_spans.remove(citation_span)

            trace_block = trace_echo(root_span)
            timings_ms["total"] = (time.perf_counter() - total_start) * 1000.0

            result = _build_result(
                request,
                resolved,
                settings,
                retrieved_chunks=pre.chunks,
                answer_text=answer_text,
                citations=citation_result.citations,
                timings_ms=timings_ms,
                trace_block=trace_block,
            )
            envelope = QueryResponse(
                result=result,
                guardrail_decisions=guardrail_decisions,
                generation_mode="live",
            )
            yield _sse_event("final", envelope.model_dump(mode="json"))
        except Exception as error:
            # Post-commit failure: NEVER retried (a retry would replay
            # partial text). The failure is recorded on the still-open
            # span(s) — the generation span when generation failed — and
            # exactly one typed error event terminates the stream.
            for span in open_spans:
                record_span_error(span, error)
            yield _sse_event("error", _stream_error_payload(error))
        finally:
            # Every path — final, error, or client disconnect (GeneratorExit)
            # — ends the open stage spans and the root span cleanly.
            for span in open_spans:
                span.end()
            root_span.end()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class _timed:
    """Context manager recording elapsed wall time (ms) into a timings dict."""

    def __init__(self, timings_ms: dict[str, float], key: str) -> None:
        self._timings_ms = timings_ms
        self._key = key

    def __enter__(self) -> None:
        self._start = time.perf_counter()

    def __exit__(self, *exc_info: object) -> None:
        self._timings_ms[self._key] = (time.perf_counter() - self._start) * 1000.0
