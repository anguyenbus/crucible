/**
 * Shared test fixtures (not a `*.test.ts`, so not collected as a suite). Builds
 * representative `final`/`error` envelopes and SSE wire text mirroring the
 * orchestrator's contract, so the browser-free suites exercise real shapes.
 */

import type {
    GuardrailDecision,
    QueryResponseEnvelope,
    QueryResult,
    RawCitation,
    RetrievedChunk,
} from "@/lib/envelope";

export function makeChunk(overrides: Partial<RetrievedChunk> = {}): RetrievedChunk {
    return {
        chunk_id: "c:1",
        rank: 1,
        score: 0.9,
        text: "Chunk text.",
        ...overrides,
    };
}

export function makeResult(overrides: Partial<QueryResult> = {}): QueryResult {
    return {
        schema_version: "1.1.0",
        system_version: {
            pipeline_version: "legal-rag-default-1.8.0",
            config_sha256: "abcdef0123456789deadbeef",
            opensearch_index: "legal-index-v3",
            generator_model: "bedrock/claude",
            embedder_model: "bedrock/titan",
        },
        query: { query_id: "q-1", text: "What is X?" },
        answer: { text: "The answer.", citations: [] },
        retrieved_chunks: [],
        timings_ms: { retrieval: 12.3, generation: 456.7, total: 500.0 },
        ...overrides,
    };
}

export function makeFinalEnvelope(
    overrides: {
        result?: Partial<QueryResult>;
        guardrail_decisions?: GuardrailDecision[];
        generation_mode?: "stub" | "live";
    } = {},
): QueryResponseEnvelope {
    return {
        result: makeResult(overrides.result),
        guardrail_decisions: overrides.guardrail_decisions ?? [],
        generation_mode: overrides.generation_mode ?? "live",
    };
}

export function citation(chunkIds: string[], span: [number, number]): RawCitation {
    return { chunk_ids: chunkIds, claim_span: span };
}

/** Render a list of {event, data} into raw SSE wire text. */
export function sseWireText(
    events: { event: string; data: unknown }[],
): string {
    return events
        .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`)
        .join("");
}

/** Split a string into byte chunks at the given cut points (chunk-boundary tests). */
export function toByteChunks(text: string, cuts: number[] = []): Uint8Array[] {
    const encoder = new TextEncoder();
    if (cuts.length === 0) return [encoder.encode(text)];
    const parts: Uint8Array[] = [];
    let prev = 0;
    for (const cut of cuts) {
        parts.push(encoder.encode(text.slice(prev, cut)));
        prev = cut;
    }
    parts.push(encoder.encode(text.slice(prev)));
    return parts;
}
