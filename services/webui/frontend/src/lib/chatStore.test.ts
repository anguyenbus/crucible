import { describe, expect, it } from "vitest";
import {
    createChat,
    defaultChatName,
    recordTurnOutcome,
    requestHistory,
    type Chat,
} from "./chatStore";
import { MAX_HISTORY_TURNS, MAX_HISTORY_TURN_CHARS } from "./history";
import { initialTurn, reduceTurn } from "./render";
import { makeFinalEnvelope } from "@/testing/fixtures";

/*
 * Group 5 — the in-memory multi-chat store + per-chat history windowing at the
 * exact demo_ui/schema bounds. Only completed turns are windowed; switching
 * chats swaps each chat's own window with zero cross-chat leakage.
 */

function completedTurn(answerText: string) {
    return reduceTurn(initialTurn(), {
        type: "final",
        envelope: makeFinalEnvelope({
            result: { answer: { text: answerText, citations: [] } },
        }),
    });
}

function withHistory(chat: Chat, turns: Chat["history"]): Chat {
    return { ...chat, history: turns };
}

describe("chat store + history windowing", () => {
    it("windows to 20 turns / 8,000 chars per turn, oldest dropped first", () => {
        // 30 user/assistant turn-pairs = 60 entries; per-turn text over the cap.
        const longText = "x".repeat(MAX_HISTORY_TURN_CHARS + 500);
        const raw: Chat["history"] = [];
        for (let i = 0; i < 30; i++) {
            raw.push({ role: "user", text: `q${i} ${longText}` });
            raw.push({ role: "assistant", text: `a${i} ${longText}` });
        }
        const chat = withHistory(createChat(1), raw);
        const window = requestHistory(chat);

        expect(window).toHaveLength(MAX_HISTORY_TURNS);
        for (const turn of window) {
            expect(turn.text.length).toBeLessThanOrEqual(MAX_HISTORY_TURN_CHARS);
        }
        // Oldest dropped first ⇒ the newest tail is kept.
        expect(window[window.length - 1].text.startsWith("a29")).toBe(true);
        expect(window[0].text.startsWith("q20")).toBe(true);
    });

    it("switching chats swaps the window with no cross-chat leakage", () => {
        const chatA = recordTurnOutcome(createChat(1), "alpha question", completedTurn("alpha answer"));
        const chatB = recordTurnOutcome(createChat(2), "beta question", completedTurn("beta answer"));

        const windowA = requestHistory(chatA);
        const windowB = requestHistory(chatB);

        expect(windowA.map((t) => t.text)).toEqual(["alpha question", "alpha answer"]);
        expect(windowB.map((t) => t.text)).toEqual(["beta question", "beta answer"]);
        // Neither window references the other chat's turns.
        expect(windowA.some((t) => t.text.includes("beta"))).toBe(false);
        expect(windowB.some((t) => t.text.includes("alpha"))).toBe(false);
    });

    it("appends only completed turns; error turns are dropped from history", () => {
        const base = createChat(1);
        const afterFinal = recordTurnOutcome(base, "q", completedTurn("done"));
        expect(afterFinal.history).toHaveLength(2);

        const errored = reduceTurn(initialTurn(), {
            type: "error",
            error: { detail: "boom", http_equivalent: 500 },
        });
        const afterError = recordTurnOutcome(afterFinal, "q2", errored);
        // History is unchanged — the errored turn never entered it.
        expect(afterError.history).toEqual(afterFinal.history);
    });

    it("gives a new chat a deterministic default name (no auto-title)", () => {
        expect(defaultChatName(1)).toBe("New chat");
        expect(defaultChatName(2)).toBe("New chat 2");
        expect(createChat(1).name).toBe("New chat");
        expect(createChat(3).name).toBe("New chat 3");
    });
});
