/**
 * TypeScript types for the SUBSET of the orchestrator's SSE contract that the
 * UI renders. Mirrored (not imported — the dependency firewall forbids any
 * `app.*` coupling) from `services/orchestrator/app/schemas/{query,envelope}.py`
 * and the eval `rag_query_output` v1.1.0 schema.
 *
 * Event vocabulary is fixed by the orchestrator:
 *   - `token` `{ text }`   — a raw generation delta (live-token configs only)
 *   - `final` (QueryResponse envelope) — the ONE terminal success event
 *   - `error` (typed) — the ONE terminal failure event
 * Every stream ends in EXACTLY ONE terminal event (`final` | `error`).
 *
 * These are plain data types with no runtime dependency, so the parser and the
 * render mapper that consume them stay browser-free testable.
 */

/** One cited claim: a `[start, end)` span in the answer plus its chunk ids. */
export interface RawCitation {
    claim_span: [number, number];
    chunk_ids: string[];
}

/** One retrieved chunk as shown in the numbered source cards. */
export interface RetrievedChunk {
    chunk_id: string;
    rank: number;
    score: number;
    text: string;
    // Other schema slots (doc_id, char_span, page_indices, …) exist on the wire
    // but the Phase-1 UI does not render them.
}

/** Config + provenance echo (`system_version` in the eval payload). */
export interface SystemVersion {
    pipeline_version: string;
    config_sha256: string;
    opensearch_index: string;
    generator_model?: string;
    embedder_model?: string;
    opensearch_host?: string;
}

/** Phoenix trace pointer — present only when tracing is real (Phase 3 use). */
export interface TraceBlock {
    trace_id: string;
    span_id?: string;
    phoenix_project?: string;
}

/** The eval `rag_query_output` payload carried verbatim inside the envelope. */
export interface QueryResult {
    schema_version: string;
    system_version: SystemVersion;
    query: { query_id: string; text: string; metadata?: Record<string, unknown> };
    answer: { text: string; answer_supported?: boolean; citations: RawCitation[] };
    retrieved_chunks: RetrievedChunk[];
    timings_ms: Record<string, number>;
    trace?: TraceBlock;
}

/** One guardrail evaluation outcome (envelope top level). */
export interface GuardrailDecision {
    stage: string;
    decision: string;
    rule_id?: string | null;
    category?: string | null;
    rationale?: string | null;
}

/** The full `final` envelope (orchestrator-owned wrapper around `result`). */
export interface QueryResponseEnvelope {
    result: QueryResult;
    guardrail_decisions: GuardrailDecision[];
    generation_mode: "stub" | "live";
}

/** The typed `error` SSE payload (and the pre-stream HTTP error shape). */
export interface StreamError {
    detail: string;
    dependency?: "opensearch" | "bedrock" | string;
    retry_after_seconds?: number;
    http_equivalent: number;
}

/** A single `token` delta. */
export interface TokenStreamEvent {
    type: "token";
    text: string;
}

/** The terminal success event. */
export interface FinalStreamEvent {
    type: "final";
    envelope: QueryResponseEnvelope;
}

/** The terminal failure event. */
export interface ErrorStreamEvent {
    type: "error";
    error: StreamError;
}

/** Discriminated union of the three rendered SSE events. */
export type StreamEvent = TokenStreamEvent | FinalStreamEvent | ErrorStreamEvent;
