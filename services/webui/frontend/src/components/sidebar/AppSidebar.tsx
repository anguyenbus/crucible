"use client";

/*
 * Adapted from references/donna/frontend/src/app/components/shared/AppSidebar.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). The Phase-1 shell: the
 * "Document Analyser" wordmark, a New chat button, and the in-memory chat list.
 * Phase 2 adds a Chat/Projects view switch so the ported BFF screens mount in
 * this same shell. Donna's account/workflows chrome is out of scope and dropped.
 */
import { FolderKanban, MessagesSquare, Plus } from "lucide-react";
import { Wordmark } from "@/components/Wordmark";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/contexts/ChatStoreContext";
import { useWorkspaceView } from "@/contexts/WorkspaceViewContext";
import { SidebarChatItem } from "./SidebarChatItem";

export function AppSidebar() {
    const { chats, activeChatId, newChat, selectChat } = useChatStore();
    const { view, setView } = useWorkspaceView();

    return (
        <aside className="flex h-full w-64 flex-col border-r border-sidebar-border bg-sidebar">
            <div className="flex items-center px-4 py-4">
                <Wordmark className="text-base" />
            </div>

            <nav className="space-y-0.5 px-3">
                <NavButton
                    active={view === "chat"}
                    onClick={() => setView("chat")}
                    icon={<MessagesSquare className="size-4" />}
                    label="Chat"
                />
                <NavButton
                    active={view === "projects"}
                    onClick={() => setView("projects")}
                    icon={<FolderKanban className="size-4" />}
                    label="Projects"
                />
            </nav>

            {view === "chat" && (
                <>
                    <div className="mt-3 px-3">
                        <Button
                            variant="outline"
                            className="w-full justify-start gap-2"
                            onClick={newChat}
                        >
                            <Plus className="size-4" />
                            New chat
                        </Button>
                    </div>
                    <div className="mt-3 flex-1 space-y-0.5 overflow-y-auto px-3 pb-4">
                        {chats.map((chat) => (
                            <SidebarChatItem
                                key={chat.id}
                                name={chat.name}
                                active={chat.id === activeChatId}
                                onSelect={() => selectChat(chat.id)}
                            />
                        ))}
                    </div>
                </>
            )}
        </aside>
    );
}

function NavButton({
    active,
    onClick,
    icon,
    label,
}: {
    active: boolean;
    onClick: () => void;
    icon: React.ReactNode;
    label: string;
}) {
    return (
        <button
            type="button"
            onClick={onClick}
            aria-current={active ? "page" : undefined}
            className={cn(
                "flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                active
                    ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
                    : "text-sidebar-foreground hover:bg-sidebar-accent/60",
            )}
        >
            {icon}
            {label}
        </button>
    );
}
