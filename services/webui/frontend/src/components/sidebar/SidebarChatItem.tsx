"use client";

/*
 * Adapted from references/donna/frontend/src/app/components/shared/SidebarChatItem.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). Backend rename/delete actions
 * are dropped — chats are in-memory only.
 */
import { cn } from "@/lib/utils";

interface SidebarChatItemProps {
    name: string;
    active: boolean;
    onSelect: () => void;
}

export function SidebarChatItem({ name, active, onSelect }: SidebarChatItemProps) {
    return (
        <button
            type="button"
            onClick={onSelect}
            aria-current={active ? "true" : undefined}
            className={cn(
                "w-full truncate rounded-md px-3 py-2 text-left text-sm transition-colors",
                active
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground hover:bg-sidebar-accent/60",
            )}
        >
            {name}
        </button>
    );
}
