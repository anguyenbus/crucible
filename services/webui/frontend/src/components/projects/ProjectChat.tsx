"use client";

/**
 * Project-scoped chat embedded as the project page's "Chat" tab.
 *
 * Runs in its OWN ChatStoreProvider + ChatScopeProvider so it never shares chats
 * or scope with the global sidebar chat. The project is LOCKED (pre-selected via
 * `initialProject`, no project picker): every turn queries this project's index.
 * The general legal index is the only scope choice (on/off). Guardrails on/off is
 * carried through from the shared config-ref toggle.
 */

import { Plus } from "lucide-react";
import {
    ChatScopeProvider,
    useChatScope,
    type SelectedProject,
} from "@/contexts/ChatScopeContext";
import { ChatStoreProvider, useChatStore } from "@/contexts/ChatStoreContext";
import { useAssistantChat } from "@/hooks/useAssistantChat";
import { useReadyz } from "@/hooks/useReadyz";
import { ChatView } from "@/components/chat/ChatView";
import { ChatInput } from "@/components/chat/ChatInput";
import { ReadyzBanner } from "@/components/chat/ReadyzBanner";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { Chat } from "@/lib/chatStore";

interface Props {
    projectId: string;
    projectName: string;
    indexName: string;
}

export function ProjectChat({ projectId, projectName, indexName }: Props) {
    const project: SelectedProject = { id: projectId, name: projectName, indexName };
    // Own ChatStoreProvider ⇒ this project's sessions are isolated from the
    // global sidebar chat AND from other projects' Chat tabs.
    return (
        <ChatStoreProvider>
            <ChatScopeProvider initialProject={project}>
                <div className="flex flex-1 overflow-hidden">
                    <ProjectChatSessions />
                    <ProjectChatBody projectName={projectName} />
                </div>
            </ChatScopeProvider>
        </ChatStoreProvider>
    );
}

/** The label for a session in the history list: its first question, else its
 *  default name (a fresh, unused session). */
function sessionLabel(chat: Chat): string {
    const firstQuestion = chat.messages.find((m) => m.role === "user")?.content.trim();
    return firstQuestion || chat.name;
}

/** Per-project session panel: "+ New chat" + a browsable list of this
 *  project's in-memory sessions, mirroring the main-page sidebar. */
function ProjectChatSessions() {
    const { chats, activeChatId, newChat, selectChat } = useChatStore();
    return (
        <div className="flex w-56 shrink-0 flex-col border-r border-border">
            <div className="p-3">
                <Button
                    variant="outline"
                    className="w-full justify-start gap-2"
                    onClick={newChat}
                >
                    <Plus className="size-4" />
                    New chat
                </Button>
            </div>
            <div className="flex-1 space-y-0.5 overflow-y-auto px-3 pb-3">
                {chats.map((chat) => (
                    <button
                        key={chat.id}
                        type="button"
                        onClick={() => selectChat(chat.id)}
                        aria-current={chat.id === activeChatId ? "true" : undefined}
                        title={sessionLabel(chat)}
                        className={cn(
                            "w-full truncate rounded-md px-3 py-2 text-left text-sm transition-colors",
                            chat.id === activeChatId
                                ? "bg-accent text-accent-foreground"
                                : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                        )}
                    >
                        {sessionLabel(chat)}
                    </button>
                ))}
            </div>
        </div>
    );
}

function ProjectChatBody({ projectName }: { projectName: string }) {
    const { activeChat } = useChatStore();
    const { send, cancel, isStreaming } = useAssistantChat();
    const readyz = useReadyz();
    const disabled = !readyz.ready;
    const hasMessages = activeChat.messages.length > 0;

    return (
        <div className="flex flex-1 flex-col overflow-hidden">
            {!readyz.ready && readyz.errorText && (
                <ReadyzBanner
                    errorText={readyz.errorText}
                    checking={readyz.checking}
                    onRetry={readyz.retry}
                />
            )}

            {hasMessages ? (
                <div className="flex-1 overflow-y-auto">
                    <ChatView messages={activeChat.messages} />
                </div>
            ) : (
                <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
                    <p className="max-w-md text-sm text-muted-foreground">
                        Ask a question about{" "}
                        <span className="font-medium text-foreground">{projectName}</span>. Answers
                        are grounded in this project&apos;s documents.
                    </p>
                </div>
            )}

            <div className="mx-auto w-full max-w-3xl px-4 pb-6">
                <ProjectChatScopeBar projectName={projectName} />
                <ChatInput
                    onSubmit={send}
                    onCancel={cancel}
                    isStreaming={isStreaming}
                    disabled={disabled}
                />
            </div>
        </div>
    );
}

function ProjectChatScopeBar({ projectName }: { projectName: string }) {
    const { includeGeneral, guardrailsOn, setIncludeGeneral, setGuardrailsOn } = useChatScope();
    return (
        <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 px-4 text-xs text-muted-foreground">
            <span>
                Scope: <span className="font-medium text-foreground">{projectName}</span> (project
                index)
            </span>
            <label className="flex items-center gap-2">
                <input
                    type="checkbox"
                    checked={includeGeneral}
                    onChange={(e) => setIncludeGeneral(e.target.checked)}
                    aria-label="Include the general legal index"
                    className="size-3.5 accent-current"
                />
                <span>Include general legal index</span>
            </label>
            <label
                className="flex items-center gap-2"
                title="ON = guarded config (1.8.0); OFF = no guards (1.2.0)"
            >
                <input
                    type="checkbox"
                    checked={guardrailsOn}
                    onChange={(e) => setGuardrailsOn(e.target.checked)}
                    aria-label="Enable guardrails"
                    className="size-3.5 accent-current"
                />
                <span>Guardrails</span>
            </label>
        </div>
    );
}
