"use client";

/*
 * Adapted from references/donna/frontend/src/app/components/assistant/ChatView.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). Reduced to the Phase-1 chat
 * thread: user + assistant messages with the honest render model.
 */
import { useEffect, useRef } from "react";
import type { ChatMessage } from "@/lib/chatStore";
import { AssistantMessage } from "./AssistantMessage";
import { UserMessage } from "./UserMessage";

export function ChatView({ messages }: { messages: ChatMessage[] }) {
    const endRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        endRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [messages]);

    return (
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
            {messages.map((message, index) =>
                message.role === "user" ? (
                    <UserMessage key={index} content={message.content} />
                ) : (
                    message.assistant && (
                        <AssistantMessage key={index} data={message.assistant} />
                    )
                ),
            )}
            <div ref={endRef} />
        </div>
    );
}
