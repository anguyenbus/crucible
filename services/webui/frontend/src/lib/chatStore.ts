/**
 * Backend-free, in-memory multi-chat store logic (Q2 user override).
 *
 * The pure core of the adapted `ChatHistoryContext`: every backend call
 * (saveChat / loadChats / replaceChatId) and the auto-title path are stripped.
 * Chats are session-scoped and lost on refresh — no persistence layer of any
 * kind (no server, no localStorage). Each chat carries its OWN visible message
 * list AND its OWN bounded request-history window; switching chats swaps both
 * with zero cross-chat leakage (a chat's window is built only from its own
 * turns). Only COMPLETED turns are appended (error/aborted turns are dropped).
 *
 * Pure and framework-free so windowing/leakage is unit-tested without a browser.
 */

import { appendCompletedTurn, boundHistory, type HistoryTurn } from "./history";
import { initialTurn, turnEntersHistory, type TurnState } from "./render";

/** One assistant turn's render state, kept on its message for display. */
export interface AssistantMessageData {
    /** The answer text (accumulated live tokens, or the buffered final answer). */
    text: string;
    turn: TurnState;
}

export interface ChatMessage {
    role: "user" | "assistant";
    /** User message text. Empty for assistant messages (see `assistant`). */
    content: string;
    /** Present on assistant messages only. */
    assistant?: AssistantMessageData;
}

export interface Chat {
    id: string;
    name: string;
    messages: ChatMessage[];
    /** Completed-turn history (oldest-first), the source of the request window. */
    history: HistoryTurn[];
}

/** Deterministic default chat name — no auto-title (Q2). */
export function defaultChatName(seq: number): string {
    return seq === 1 ? "New chat" : `New chat ${seq}`;
}

let idCounter = 0;
function nextId(): string {
    idCounter += 1;
    return `chat-${idCounter}-${Date.now().toString(36)}`;
}

export function createChat(seq: number): Chat {
    return {
        id: nextId(),
        name: defaultChatName(seq),
        messages: [],
        history: [],
    };
}

/** The display text for an assistant turn: final answer, else live partial. */
export function assistantDisplayText(turn: TurnState): string {
    return turn.final ? turn.final.answerText : turn.streamedText;
}

/**
 * The bounded `history` to send with the NEXT request for this chat — built
 * ONLY from this chat's own completed turns (no cross-chat leakage), truncated
 * client-side to the schema bounds (20 turns / 8,000 chars per turn).
 */
export function requestHistory(chat: Chat): HistoryTurn[] {
    return boundHistory(chat.history);
}

/** Append the user question plus a fresh streaming assistant message. */
export function pushUserAndAssistant(chat: Chat, question: string): Chat {
    return {
        ...chat,
        messages: [
            ...chat.messages,
            { role: "user", content: question },
            {
                role: "assistant",
                content: "",
                assistant: { text: "", turn: initialTurn() },
            },
        ],
    };
}

/** Replace the last assistant message's turn (and its display text). */
export function setLastAssistantTurn(chat: Chat, turn: TurnState): Chat {
    const messages = [...chat.messages];
    for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === "assistant") {
            messages[i] = {
                ...messages[i],
                assistant: { text: assistantDisplayText(turn), turn },
            };
            break;
        }
    }
    return { ...chat, messages };
}

/**
 * Record a finished turn's outcome into the chat's history. Appends the
 * user/assistant pair ONLY when the turn completed with a `final`; an errored or
 * aborted turn leaves history unchanged (dishonest context is never resent).
 */
export function recordTurnOutcome(
    chat: Chat,
    question: string,
    turn: TurnState,
): Chat {
    if (!turnEntersHistory(turn) || turn.final === null) return chat;
    return {
        ...chat,
        history: appendCompletedTurn(chat.history, question, turn.final.answerText),
    };
}
