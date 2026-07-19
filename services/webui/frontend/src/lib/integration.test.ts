import { describe, expect, it, vi } from "vitest";
import { streamQuery } from "./streamClient";
import {
    abortTurn,
    guardrailChip,
    initialTurn,
    reduceTurn,
    turnEntersHistory,
    type TurnState,
} from "./render";
import { numberCitations } from "./citations";
import { createChat, recordTurnOutcome, requestHistory } from "./chatStore";
import type { RawCitation, RetrievedChunk, StreamEvent } from "./envelope";
import { citation, makeChunk, makeFinalEnvelope, sseWireText } from "@/testing/fixtures";

/*
 * Group 8 — strategic end-to-end flows across the seams: the SSE client -> the
 * turn state machine -> the render model / history / citation mapper. These
 * exercise the highest-value workflows the unit suites cover only in pieces.
 */

function eventStreamResponse(wire: string): Response {
    const stream = new ReadableStream<Uint8Array>({
        start(controller) {
            controller.enqueue(new TextEncoder().encode(wire));
            controller.close();
        },
    });
    return new Response(stream, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
    });
}

async function drive(
    wire: string,
    fetchImpl: typeof fetch,
): Promise<{ turn: TurnState; events: StreamEvent[] }> {
    let turn = initialTurn();
    const events: StreamEvent[] = [];
    await streamQuery(
        { question: "q", history: [], pipelineConfig: "legal-rag-default-1.8.0" },
        (event) => {
            events.push(event);
            turn = reduceTurn(turn, event);
        },
        new AbortController().signal,
        fetchImpl,
    );
    return { turn, events };
}

describe("end-to-end flows", () => {
    it("buffered-final with guardrail_decisions renders the chip + refusal without hanging, and enters history", async () => {
        const wire = sseWireText([
            {
                event: "final",
                data: makeFinalEnvelope({
                    result: { answer: { text: "I can't help with that request.", citations: [] } },
                    guardrail_decisions: [
                        { stage: "input", decision: "block", rule_id: "prompt_injection", category: "denied_topic" },
                    ],
                }),
            },
        ]);
        const fetchImpl = vi.fn(async () => eventStreamResponse(wire)) as unknown as typeof fetch;

        const { turn, events } = await drive(wire, fetchImpl);
        // Zero token events (buffered), one terminal final — no hang.
        expect(events.filter((e) => e.type === "token")).toHaveLength(0);
        expect(turn.phase).toBe("final");
        expect(turn.sawToken).toBe(false);
        expect(guardrailChip(turn.final!.guardrailDecisions)).toEqual({ action: "block", ruleCount: 1 });

        // The completed (refusal) turn enters the chat's bounded history.
        const chat = recordTurnOutcome(createChat(1), "q", turn);
        expect(requestHistory(chat).map((t) => t.text)).toEqual([
            "q",
            "I can't help with that request.",
        ]);
    });

    it("live-token flow aborted mid-stream: partial INCOMPLETE, turn dropped, upstream cancelled", async () => {
        const controller = new AbortController();
        let upstreamSignal: AbortSignal | undefined;
        const fetchImpl = (async (_url: string, init?: RequestInit) => {
            upstreamSignal = init?.signal ?? undefined;
            const stream = new ReadableStream<Uint8Array>({
                start(c) {
                    c.enqueue(new TextEncoder().encode('event: token\ndata: {"text":"partial answer"}\n\n'));
                },
                pull() {
                    // Never completes until the client aborts the request.
                    return new Promise<void>((_, reject) => {
                        init?.signal?.addEventListener("abort", () =>
                            reject(new DOMException("aborted", "AbortError")),
                        );
                    });
                },
            });
            return new Response(stream, {
                status: 200,
                headers: { "content-type": "text/event-stream" },
            });
        }) as unknown as typeof fetch;

        let turn = initialTurn();
        const pending = streamQuery(
            { question: "q", history: [], pipelineConfig: "legal-rag-default-1.2.0" },
            (event) => {
                turn = reduceTurn(turn, event);
            },
            controller.signal,
            fetchImpl,
        ).catch((error) => error);

        await vi.waitFor(() => expect(turn.sawToken).toBe(true));
        // Stop: aborting the client request cancels the upstream stream.
        controller.abort();
        const outcome = await pending;
        expect(outcome).toBeInstanceOf(DOMException);
        expect(upstreamSignal?.aborted).toBe(true);

        turn = abortTurn(turn);
        expect(turn.incomplete).toBe(true);
        expect(turn.streamedText).toBe("partial answer");
        expect(turnEntersHistory(turn)).toBe(false);

        // The dropped turn leaves history empty.
        const chat = recordTurnOutcome(createChat(1), "q", turn);
        expect(requestHistory(chat)).toHaveLength(0);
    });

    it("citation degradation end-to-end: a cited chunk absent from retrieved_chunks stays plain text", async () => {
        const wire = sseWireText([
            { event: "token", data: { text: "See " } },
            { event: "token", data: { text: "[c:1] and [c:missing]." } },
            {
                event: "final",
                data: makeFinalEnvelope({
                    result: {
                        answer: {
                            text: "See [c:1] and [c:missing].",
                            citations: [citation(["c:1"], [4, 9]), citation(["c:missing"], [14, 25])],
                        },
                        retrieved_chunks: [makeChunk({ chunk_id: "c:1", rank: 1, text: "Evidence one." })],
                    },
                }),
            },
        ]);
        const fetchImpl = vi.fn(async () => eventStreamResponse(wire)) as unknown as typeof fetch;

        const { turn } = await drive(wire, fetchImpl);
        const final = turn.final!;
        const raw: RawCitation[] = final.citations.map((c) => ({ chunk_ids: c.chunkIds, claim_span: [c.start, c.end] }));
        const chunks: RetrievedChunk[] = final.sources.map((s) => ({
            chunk_id: s.chunkId,
            rank: s.rank,
            score: s.score,
            text: s.text,
        }));
        const numbered = numberCitations(final.answerText, raw, chunks);

        // c:1 has evidence ⇒ becomes [1]; c:missing has none ⇒ stays raw.
        expect(numbered.text).toBe("See [1] and [c:missing].");
        expect(numbered.sources).toHaveLength(1);
        expect(numbered.validNumbers.has(1)).toBe(true);
    });
});
