import { describe, expect, it } from "vitest";
import {
    abortTurn,
    guardrailChip,
    initialTurn,
    isWaitingOnBufferedGeneration,
    mapFinalEnvelope,
    reduceTurn,
    turnEntersHistory,
    type TurnState,
} from "./render";
import { makeChunk, makeFinalEnvelope } from "@/testing/fixtures";
import type { StreamEvent } from "./envelope";

/*
 * Group 6 — the honest chat render model / per-turn state machine. Both
 * streaming branches are first-class; error and Stop drop the turn; the
 * guardrail chip appears iff decisions exist.
 */

function feed(events: StreamEvent[]): TurnState {
    return events.reduce(reduceTurn, initialTurn());
}

describe("turn state machine", () => {
    it("live-token branch: real deltas accumulate as received (no drip, no synthesis)", () => {
        const afterTokens = feed([
            { type: "token", text: "Hel" },
            { type: "token", text: "lo " },
            { type: "token", text: "world" },
        ]);
        expect(afterTokens.sawToken).toBe(true);
        expect(afterTokens.streamedText).toBe("Hello world");
        expect(isWaitingOnBufferedGeneration(afterTokens)).toBe(false);

        const final = reduceTurn(afterTokens, {
            type: "final",
            envelope: makeFinalEnvelope({
                result: { answer: { text: "Hello world", citations: [] } },
            }),
        });
        expect(final.phase).toBe("final");
        expect(final.final?.answerText).toBe("Hello world");
    });

    it("buffered-final branch: zero tokens ⇒ waiting state, then the full answer at once", () => {
        const start = initialTurn();
        // No token events yet ⇒ the honest waiting-on-guarded-generation state.
        expect(isWaitingOnBufferedGeneration(start)).toBe(true);

        const final = reduceTurn(start, {
            type: "final",
            envelope: makeFinalEnvelope({
                result: { answer: { text: "Full buffered answer.", citations: [] } },
            }),
        });
        expect(final.sawToken).toBe(false);
        expect(final.streamedText).toBe(""); // never replayed as fake tokens
        expect(final.final?.answerText).toBe("Full buffered answer.");
        expect(turnEntersHistory(final)).toBe(true);
    });

    it("error branch: partial text marked INCOMPLETE and the turn is dropped from history", () => {
        const state = feed([
            { type: "token", text: "partial…" },
            { type: "error", error: { detail: "throttled", dependency: "bedrock", retry_after_seconds: 5, http_equivalent: 503 } },
        ]);
        expect(state.phase).toBe("error");
        expect(state.incomplete).toBe(true);
        expect(state.streamedText).toBe("partial…");
        expect(state.error).toMatchObject({ detail: "throttled", dependency: "bedrock", retryAfterSeconds: 5 });
        expect(turnEntersHistory(state)).toBe(false);
    });

    it("Stop/abort applies error semantics: partial INCOMPLETE, turn dropped", () => {
        const streaming = feed([{ type: "token", text: "half an answer" }]);
        const aborted = abortTurn(streaming);
        expect(aborted.phase).toBe("aborted");
        expect(aborted.incomplete).toBe(true);
        expect(turnEntersHistory(aborted)).toBe(false);

        // Aborting an already-final turn is a no-op (it stays completed).
        const done = reduceTurn(initialTurn(), { type: "final", envelope: makeFinalEnvelope() });
        expect(abortTurn(done).phase).toBe("final");
    });

    it("guardrail chip renders iff decisions are non-empty (incl. the refusal final)", () => {
        expect(guardrailChip([])).toBeNull();

        const refusal = reduceTurn(initialTurn(), {
            type: "final",
            envelope: makeFinalEnvelope({
                result: { answer: { text: "Refused.", citations: [] } },
                guardrail_decisions: [{ stage: "input", decision: "block", rule_id: "injection" }],
            }),
        });
        expect(refusal.sawToken).toBe(false); // zero tokens, renders without hanging
        const chip = guardrailChip(refusal.final!.guardrailDecisions);
        expect(chip).toEqual({ action: "block", ruleCount: 1 });

        // Highest-precedence action wins across multiple decisions.
        expect(
            guardrailChip([
                { stage: "output", decision: "flag" },
                { stage: "output", decision: "transform" },
            ]),
        ).toEqual({ action: "transform", ruleCount: 2 });
    });

    it("maps the final envelope's citations, sources, timings and provenance", () => {
        const envelope = makeFinalEnvelope({
            result: {
                answer: {
                    text: "Alpha [c:1] statement.",
                    citations: [{ chunk_ids: ["c:1"], claim_span: [0, 5] }],
                },
                retrieved_chunks: [makeChunk({ chunk_id: "c:1", rank: 1, score: 0.88, text: "Evidence." })],
            },
        });
        const render = mapFinalEnvelope(envelope);
        expect(render.pipelineVersion).toBe("legal-rag-default-1.8.0");
        expect(render.configSha256Short).toBe("abcdef012345");
        expect(render.index).toBe("legal-index-v3");
        expect(render.citations[0]).toMatchObject({ chunkIds: ["c:1"], claimText: "Alpha" });
        expect(render.sources[0]).toMatchObject({ chunkId: "c:1", rank: 1, score: 0.88 });
        expect(render.timingsMs.total).toBe(500);
    });
});
