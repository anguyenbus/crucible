import { describe, expect, it } from "vitest";
import { typedEventsFromByteChunks } from "./sse";
import type { StreamEvent } from "./envelope";
import {
    makeFinalEnvelope,
    sseWireText,
    toByteChunks,
} from "@/testing/fixtures";

/*
 * Group 3 — SSE line parser (the demo_ui/sse.py pattern), browser-free. Both
 * the buffered-final and the input-guard refusal paths are first-class, and the
 * single-terminal-event rule holds.
 */

describe("SSE parser", () => {
    it("frames token/final/error in order across arbitrary chunk splits", () => {
        const wire = sseWireText([
            { event: "token", data: { text: "He" } },
            { event: "token", data: { text: "llo" } },
            { event: "final", data: makeFinalEnvelope({ result: { answer: { text: "Hello", citations: [] } } }) },
        ]);
        // Cut the byte stream mid-frame in several places.
        const chunks = toByteChunks(wire, [5, 12, 30, wire.length - 3]);
        const events = typedEventsFromByteChunks(chunks);

        expect(events.map((e) => e.type)).toEqual(["token", "token", "final"]);
        expect((events[0] as Extract<StreamEvent, { type: "token" }>).text).toBe("He");
        expect((events[1] as Extract<StreamEvent, { type: "token" }>).text).toBe("llo");
        const final = events[2] as Extract<StreamEvent, { type: "final" }>;
        expect(final.envelope.result.answer.text).toBe("Hello");
    });

    it("buffered branch: zero token events, exactly one final", () => {
        const wire = sseWireText([
            { event: "final", data: makeFinalEnvelope() },
        ]);
        const events = typedEventsFromByteChunks(toByteChunks(wire));

        expect(events).toHaveLength(1);
        expect(events[0].type).toBe("final");
    });

    it("input-guard refusal: one final with populated guardrail_decisions, zero tokens", () => {
        const envelope = makeFinalEnvelope({
            result: { answer: { text: "I can't help with that.", citations: [] } },
            guardrail_decisions: [
                { stage: "input", decision: "block", rule_id: "prompt_injection" },
            ],
        });
        const wire = sseWireText([{ event: "final", data: envelope }]);
        const events = typedEventsFromByteChunks(toByteChunks(wire, [10, 40]));

        expect(events).toHaveLength(1);
        const final = events[0] as Extract<StreamEvent, { type: "final" }>;
        expect(final.envelope.guardrail_decisions).toHaveLength(1);
        expect(final.envelope.guardrail_decisions[0].decision).toBe("block");
    });

    it("terminal-event rule: the stream is complete on exactly one final OR one error", () => {
        const finalWire = sseWireText([
            { event: "token", data: { text: "hi" } },
            { event: "final", data: makeFinalEnvelope() },
        ]);
        const finalEvents = typedEventsFromByteChunks(toByteChunks(finalWire));
        const terminals = finalEvents.filter((e) => e.type === "final" || e.type === "error");
        expect(terminals).toHaveLength(1);
        expect(terminals[0].type).toBe("final");

        const errorWire = sseWireText([
            { event: "error", data: { detail: "boom", http_equivalent: 500 } },
        ]);
        const errorEvents = typedEventsFromByteChunks(toByteChunks(errorWire));
        expect(errorEvents.filter((e) => e.type === "error")).toHaveLength(1);
    });

    it("error event parses the typed taxonomy fields", () => {
        const wire = sseWireText([
            {
                event: "error",
                data: {
                    detail: "Bedrock throttling persisted.",
                    dependency: "bedrock",
                    retry_after_seconds: 5,
                    http_equivalent: 503,
                },
            },
        ]);
        const events = typedEventsFromByteChunks(toByteChunks(wire));
        expect(events).toHaveLength(1);
        const error = events[0] as Extract<StreamEvent, { type: "error" }>;
        expect(error.error.detail).toBe("Bedrock throttling persisted.");
        expect(error.error.dependency).toBe("bedrock");
        expect(error.error.retry_after_seconds).toBe(5);
        expect(error.error.http_equivalent).toBe(503);
    });

    it("ignores SSE comment/keep-alive lines", () => {
        const wire =
            ": keep-alive\n" + sseWireText([{ event: "final", data: makeFinalEnvelope() }]);
        const events = typedEventsFromByteChunks(toByteChunks(wire));
        expect(events).toHaveLength(1);
        expect(events[0].type).toBe("final");
    });
});
