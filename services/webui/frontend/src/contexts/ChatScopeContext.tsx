"use client";

import {
    createContext,
    useContext,
    useMemo,
    useState,
    type ReactNode,
} from "react";

/**
 * Project-scoped chat state (spec req 7): which project the chat is scoped to
 * and whether the general index is additionally included.
 *
 * - `project` null ⇒ general chat (no project selected): `retrieval_indices` is
 *   omitted and the orchestrator keeps its single-`legal-rag-bench` default.
 * - `includeGeneral` defaults to OFF (project-only) so answers are grounded in
 *   the user's uploaded docs; it is per-session UI state, NOT persisted.
 *
 * This is separate from the in-memory multi-chat store (which is unchanged):
 * it carries ONLY the retrieval scope the send hook reads per turn.
 */
export interface SelectedProject {
    id: string;
    name: string;
    /** The project's `index_name` (`proj-{id}`) from the BFF record. */
    indexName: string;
}

interface ChatScopeValue {
    project: SelectedProject | null;
    includeGeneral: boolean;
    /**
     * Guardrails toggle (default ON). ON runs the guarded config
     * (`getConfigRef`, default 1.8.0); OFF runs the unguarded config
     * (default 1.2.0). Per-session UI state, NOT persisted; the send hook
     * maps it to `pipeline_config` via `resolveConfigRef`.
     */
    guardrailsOn: boolean;
    setProject: (project: SelectedProject | null) => void;
    setIncludeGeneral: (include: boolean) => void;
    setGuardrailsOn: (on: boolean) => void;
}

const ChatScopeContext = createContext<ChatScopeValue | undefined>(undefined);

export function ChatScopeProvider({
    children,
    initialProject = null,
}: {
    children: ReactNode;
    /** Pre-select a project (e.g. the embedded project-page Chat tab, which is
     *  permanently scoped to one project and never renders a project picker). */
    initialProject?: SelectedProject | null;
}) {
    const [project, setProject] = useState<SelectedProject | null>(initialProject);
    // DEFAULT OFF: project docs only until the user opts into the general index.
    const [includeGeneral, setIncludeGeneral] = useState(false);
    // DEFAULT ON: guards run unless the user explicitly turns them off.
    const [guardrailsOn, setGuardrailsOn] = useState(true);
    const value = useMemo(
        () => ({
            project,
            includeGeneral,
            guardrailsOn,
            setProject,
            setIncludeGeneral,
            setGuardrailsOn,
        }),
        [project, includeGeneral, guardrailsOn],
    );
    return (
        <ChatScopeContext.Provider value={value}>
            {children}
        </ChatScopeContext.Provider>
    );
}

export function useChatScope(): ChatScopeValue {
    const context = useContext(ChatScopeContext);
    if (!context) {
        throw new Error("useChatScope must be used within a ChatScopeProvider");
    }
    return context;
}
