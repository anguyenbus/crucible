/**
 * Envelope -> render-model mapping and the per-turn stream STATE MACHINE.
 *
 * The TS mirror of the orchestrator Chainlit `demo_ui/chat_ui/render.py` +
 * `history.py` honesty rules. Everything the UI shows about a completed turn is
 * derived here from the `final` envelope — never synthesized client-side:
 *   - citations / numbered source cards / `timings_ms` / provenance render ONLY
 *     from `final`; zero citations is an honest state.
 *   - live `token` deltas accumulate as REAL text (no simulated streaming, no
 *     drip pacing); the buffered branch shows an honest waiting state (zero
 *     tokens) then the full answer at once.
 *   - on `error` OR user abort the partial text stays visible marked INCOMPLETE
 *     and the turn is dropped from history.
 *   - the guardrail chip is derived here and renders ONLY when
 *     `guardrail_decisions` is non-empty (including the input-guard refusal
 *     `final`, which carries zero tokens).
 *
 * Pure and framework-free so the state machine is unit-tested without a browser.
 */

import type {
    GuardrailDecision,
    QueryResponseEnvelope,
    StreamError,
    StreamEvent,
} from "./envelope";

const SHA_SHORT_LEN = 12;

export interface RenderedCitation {
    chunkIds: string[];
    start: number;
    end: number;
    claimText: string;
}

export interface RenderedSource {
    rank: number;
    chunkId: string;
    score: number;
    text: string;
}

/** Everything the UI renders after the `final` event. */
export interface FinalRender {
    answerText: string;
    citations: RenderedCitation[];
    sources: RenderedSource[];
    timingsMs: Record<string, number>;
    /** The LIVE pinned config ref, shown in provenance to distinguish modes. */
    pipelineVersion: string;
    configSha256Short: string;
    index: string;
    guardrailDecisions: GuardrailDecision[];
    generationMode: string;
}

/** Typed render of one SSE `error` event (or a pre-stream HTTP error). */
export interface ErrorRender {
    detail: string;
    dependency?: string;
    retryAfterSeconds?: number;
    httpEquivalent?: number;
}

export function retryGuidance(error: ErrorRender): string {
    if (error.retryAfterSeconds !== undefined) {
        return `Transient — retry in about ${error.retryAfterSeconds} seconds.`;
    }
    return "Check the dependency named above, then retry.";
}

/** Map the `final` envelope's QueryResponse to the render model (final-only). */
export function mapFinalEnvelope(envelope: QueryResponseEnvelope): FinalRender {
    const result = envelope.result;
    const answerText = result.answer.text;

    const citations: RenderedCitation[] = result.answer.citations.map((raw) => {
        const [start, end] = raw.claim_span;
        return {
            chunkIds: [...raw.chunk_ids],
            start,
            end,
            claimText: answerText.slice(start, end).trim(),
        };
    });

    const sources: RenderedSource[] = result.retrieved_chunks.map((chunk) => ({
        rank: chunk.rank,
        chunkId: chunk.chunk_id,
        score: Number(chunk.score),
        text: chunk.text,
    }));

    const sv = result.system_version;
    return {
        answerText,
        citations,
        sources,
        timingsMs: { ...result.timings_ms },
        pipelineVersion: sv.pipeline_version,
        configSha256Short: (sv.config_sha256 ?? "").slice(0, SHA_SHORT_LEN),
        index: sv.opensearch_index,
        guardrailDecisions: [...(envelope.guardrail_decisions ?? [])],
        generationMode: envelope.generation_mode,
    };
}

export function mapErrorEvent(error: StreamError): ErrorRender {
    return {
        detail: error.detail,
        dependency: error.dependency,
        retryAfterSeconds: error.retry_after_seconds,
        httpEquivalent: error.http_equivalent,
    };
}

/** The minimal guardrail chip: highest-precedence action + rule count. */
export interface GuardrailChip {
    action: string;
    ruleCount: number;
}

const ACTION_PRECEDENCE = ["block", "transform", "flag", "allow"];

/**
 * Derive the chip ONLY when `guardrail_decisions` is non-empty; otherwise null
 * (nothing renders). `action` is the highest-precedence decision verb present;
 * `ruleCount` is the number of decisions (rules that fired).
 */
export function guardrailChip(decisions: GuardrailDecision[]): GuardrailChip | null {
    if (!decisions || decisions.length === 0) return null;
    let action = decisions[0].decision;
    let bestRank = ACTION_PRECEDENCE.indexOf(action);
    if (bestRank === -1) bestRank = ACTION_PRECEDENCE.length;
    for (const decision of decisions) {
        let rank = ACTION_PRECEDENCE.indexOf(decision.decision);
        if (rank === -1) rank = ACTION_PRECEDENCE.length;
        if (rank < bestRank) {
            bestRank = rank;
            action = decision.decision;
        }
    }
    return { action, ruleCount: decisions.length };
}

// ---------------------------------------------------------------------------
// Per-turn stream state machine
// ---------------------------------------------------------------------------

export type TurnPhase = "streaming" | "final" | "error" | "aborted";

export interface TurnState {
    phase: TurnPhase;
    /** Raw token deltas accumulated so far (live branch); "" in buffered mode. */
    streamedText: string;
    /** True once at least one `token` event has been seen (⇒ live branch). */
    sawToken: boolean;
    final: FinalRender | null;
    error: ErrorRender | null;
    /** Partial text is present but the turn did NOT complete cleanly. */
    incomplete: boolean;
}

export function initialTurn(): TurnState {
    return {
        phase: "streaming",
        streamedText: "",
        sawToken: false,
        final: null,
        error: null,
        incomplete: false,
    };
}

/** True while streaming with no tokens yet: the honest buffered waiting state. */
export function isWaitingOnBufferedGeneration(state: TurnState): boolean {
    return state.phase === "streaming" && !state.sawToken;
}

/** Fold one SSE event into the turn state. Terminal events end the turn. */
export function reduceTurn(state: TurnState, event: StreamEvent): TurnState {
    switch (event.type) {
        case "token":
            return {
                ...state,
                streamedText: state.streamedText + event.text,
                sawToken: true,
            };
        case "final":
            return {
                ...state,
                phase: "final",
                final: mapFinalEnvelope(event.envelope),
                incomplete: false,
            };
        case "error":
            return {
                ...state,
                phase: "error",
                error: mapErrorEvent(event.error),
                incomplete: state.streamedText.length > 0,
            };
    }
}

/** Apply user-abort (Stop) semantics — same as error: INCOMPLETE, dropped. */
export function abortTurn(state: TurnState): TurnState {
    if (state.phase === "final") return state; // already completed
    return {
        ...state,
        phase: "aborted",
        incomplete: state.streamedText.length > 0,
    };
}

/** Build a terminal error turn from a pre-stream HTTP error (no SSE body). */
export function httpErrorTurn(error: StreamError): TurnState {
    return {
        phase: "error",
        streamedText: "",
        sawToken: false,
        final: null,
        error: mapErrorEvent(error),
        incomplete: false,
    };
}

/** A turn enters history ONLY when it completed with a `final`. */
export function turnEntersHistory(state: TurnState): boolean {
    return state.phase === "final" && state.final !== null;
}
