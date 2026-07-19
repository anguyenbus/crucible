/**
 * Per-chat conversation history: wire shape + client-side windowing.
 *
 * The TS mirror of the orchestrator's Chainlit `demo_ui/chat_ui/history.py`.
 * Turns use the WIRE shape of the orchestrator's `history` request field
 * (`{ role: "user" | "assistant", text }`) so the list is sent verbatim. The
 * bounds below MIRROR the schema constants in
 * `services/orchestrator/app/schemas/query.py` (MAX_HISTORY_TURNS /
 * MAX_HISTORY_TURN_CHARS) — duplicated by design, never imported (the
 * dependency firewall forbids `app.*`); the schema rejects violations with 422,
 * so the client truncates to the SAME bounds before sending.
 *
 * Turns are appended ONLY after a COMPLETED turn (a stream that ended in
 * `final`); a turn that ended in `error` or was aborted never enters history —
 * resending text the model never finished would be dishonest context.
 */

export const MAX_HISTORY_TURNS = 20;
export const MAX_HISTORY_TURN_CHARS = 8_000;

export interface HistoryTurn {
    role: "user" | "assistant";
    text: string;
}

/**
 * Truncate to the schema bounds: per-turn chars first, then oldest turns first.
 * Each turn's text is clipped to MAX_HISTORY_TURN_CHARS; when more than
 * MAX_HISTORY_TURNS remain, the OLDEST are dropped (oldest-first list, newest
 * tail kept).
 */
export function boundHistory(history: HistoryTurn[]): HistoryTurn[] {
    const clipped = history.map((turn) => ({
        role: turn.role,
        text: turn.text.slice(0, MAX_HISTORY_TURN_CHARS),
    }));
    return clipped.slice(-MAX_HISTORY_TURNS);
}

/**
 * Return the new history after ONE completed turn: append the user question and
 * the final answer text, then re-apply the bounds. MUST NOT be called for a
 * turn that ended in `error` or was aborted.
 */
export function appendCompletedTurn(
    history: HistoryTurn[],
    question: string,
    answerText: string,
): HistoryTurn[] {
    return boundHistory([
        ...history,
        { role: "user", text: question },
        { role: "assistant", text: answerText },
    ]);
}
