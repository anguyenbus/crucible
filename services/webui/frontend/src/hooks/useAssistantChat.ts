"use client";

import { useCallback, useRef, useState } from "react";
import { useChatStore } from "@/contexts/ChatStoreContext";
import { useChatScope } from "@/contexts/ChatScopeContext";
import { resolveConfigRef } from "@/lib/config";
import { buildRetrievalIndices } from "@/lib/retrievalScope";
import { streamQuery } from "@/lib/streamClient";
import {
    pushUserAndAssistant,
    recordTurnOutcome,
    requestHistory,
    setLastAssistantTurn,
} from "@/lib/chatStore";
import { abortTurn, initialTurn, reduceTurn, type TurnState } from "@/lib/render";

/**
 * Adapted from donna's `useAssistantChat`. The `DRIP_CHARS_PER_TICK` typewriter
 * animation is STRIPPED — live `token` deltas render as REAL text, exactly as
 * received (honesty rule). All render-model transitions run through the pure
 * `render.ts` state machine; this hook only owns the fetch lifecycle, the abort
 * controller, and writing each transition back into the in-memory chat.
 */
export function useAssistantChat() {
    const { activeChatId, updateChat } = useChatStore();
    const { project, includeGeneral, guardrailsOn } = useChatScope();
    const [isStreaming, setIsStreaming] = useState(false);
    const abortRef = useRef<AbortController | null>(null);

    const send = useCallback(
        async (question: string) => {
            const trimmed = question.trim();
            if (!trimmed || isStreaming) return;

            const chatId = activeChatId;
            const history = (() => {
                let window: ReturnType<typeof requestHistory> = [];
                updateChat(chatId, (chat) => {
                    window = requestHistory(chat);
                    return pushUserAndAssistant(chat, trimmed);
                });
                return window;
            })();

            let turn: TurnState = initialTurn();
            const applyTurn = (next: TurnState) => {
                turn = next;
                updateChat(chatId, (chat) => setLastAssistantTurn(chat, next));
            };

            const controller = new AbortController();
            abortRef.current = controller;
            setIsStreaming(true);

            try {
                await streamQuery(
                    {
                        question: trimmed,
                        history,
                        // Guardrails toggle → pipeline config: ON = guarded
                        // (1.8.0), OFF = unguarded (1.2.0). Provenance shows the
                        // live ref, so which one ran is always visible.
                        pipelineConfig: resolveConfigRef(guardrailsOn),
                        // Project-scoped chat: compose the retrieval index scope
                        // from the selected project + the general-index toggle.
                        // Undefined (no project) omits the field -> orchestrator
                        // default single index, unchanged.
                        retrievalIndices: buildRetrievalIndices({
                            projectIndex: project?.indexName ?? null,
                            includeGeneral,
                        }),
                    },
                    (event) => applyTurn(reduceTurn(turn, event)),
                    controller.signal,
                );
                // Completed turns (final) enter history; error turns do not.
                updateChat(chatId, (chat) => recordTurnOutcome(chat, trimmed, turn));
            } catch (error) {
                // Abort (Stop) applies the same semantics as error: the partial
                // text stays visible marked INCOMPLETE and the turn is dropped.
                if (error instanceof DOMException && error.name === "AbortError") {
                    applyTurn(abortTurn(turn));
                } else {
                    throw error;
                }
            } finally {
                abortRef.current = null;
                setIsStreaming(false);
            }
        },
        [activeChatId, isStreaming, updateChat, project, includeGeneral, guardrailsOn],
    );

    const cancel = useCallback(() => {
        abortRef.current?.abort();
    }, []);

    return { send, cancel, isStreaming };
}
