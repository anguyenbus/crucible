"use client";

import { ChatStoreProvider, useChatStore } from "@/contexts/ChatStoreContext";
import { ChatScopeProvider } from "@/contexts/ChatScopeContext";
import {
    WorkspaceViewProvider,
    useWorkspaceView,
} from "@/contexts/WorkspaceViewContext";
import { useAssistantChat } from "@/hooks/useAssistantChat";
import { useReadyz } from "@/hooks/useReadyz";
import { AppSidebar } from "@/components/sidebar/AppSidebar";
import { ChatView } from "@/components/chat/ChatView";
import { ChatInput } from "@/components/chat/ChatInput";
import { ChatScopeBar } from "@/components/chat/ChatScopeBar";
import { InitialView } from "@/components/chat/InitialView";
import { ReadyzBanner } from "@/components/chat/ReadyzBanner";
import { ProjectsWorkspace } from "@/components/projects/ProjectsWorkspace";

function ChatMain() {
    const { activeChat } = useChatStore();
    const { send, cancel, isStreaming } = useAssistantChat();
    const readyz = useReadyz();
    const disabled = !readyz.ready;
    const hasMessages = activeChat.messages.length > 0;

    return (
        <>
            {!readyz.ready && readyz.errorText && (
                <ReadyzBanner
                    errorText={readyz.errorText}
                    checking={readyz.checking}
                    onRetry={readyz.retry}
                />
            )}

            {hasMessages ? (
                <>
                    <div className="flex-1 overflow-y-auto">
                        <ChatView messages={activeChat.messages} />
                    </div>
                    <div className="mx-auto w-full max-w-3xl px-4 pb-6">
                        <ChatScopeBar />
                        <ChatInput
                            onSubmit={send}
                            onCancel={cancel}
                            isStreaming={isStreaming}
                            disabled={disabled}
                        />
                    </div>
                </>
            ) : (
                <div className="flex h-full w-full flex-col">
                    <div className="flex flex-1 flex-col">
                        <InitialView
                            onSubmit={send}
                            onCancel={cancel}
                            isStreaming={isStreaming}
                            disabled={disabled}
                        />
                    </div>
                    <div className="mx-auto w-full max-w-3xl px-4 pb-6">
                        <ChatScopeBar />
                    </div>
                </div>
            )}
        </>
    );
}

function Workspace() {
    const { view } = useWorkspaceView();
    return (
        <div className="flex h-dvh w-full">
            <AppSidebar />
            <main className="flex min-w-0 flex-1 flex-col">
                {view === "chat" ? <ChatMain /> : <ProjectsWorkspace />}
            </main>
        </div>
    );
}

export default function Home() {
    return (
        <ChatStoreProvider>
            <ChatScopeProvider>
                <WorkspaceViewProvider>
                    <Workspace />
                </WorkspaceViewProvider>
            </ChatScopeProvider>
        </ChatStoreProvider>
    );
}
