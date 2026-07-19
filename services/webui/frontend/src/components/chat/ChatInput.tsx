"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";
import { ArrowUp, Square } from "lucide-react";
import { Button } from "@/components/ui/button";

/*
 * Adapted from references/donna/frontend/src/app/components/assistant/ChatInput.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). Donna's model toggle, project
 * picker, and document attach controls are out of Phase-1 scope and dropped;
 * the stop/abort control is KEPT.
 */
interface ChatInputProps {
    onSubmit: (text: string) => void;
    onCancel: () => void;
    isStreaming: boolean;
    disabled?: boolean;
}

export function ChatInput({ onSubmit, onCancel, isStreaming, disabled }: ChatInputProps) {
    const [value, setValue] = useState("");

    const submit = () => {
        const text = value.trim();
        if (!text || isStreaming || disabled) return;
        onSubmit(text);
        setValue("");
    };

    const handleSubmit = (event: FormEvent) => {
        event.preventDefault();
        submit();
    };

    const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
        }
    };

    return (
        <form
            onSubmit={handleSubmit}
            className="flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-sm"
        >
            <textarea
                value={value}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={disabled}
                rows={1}
                placeholder={
                    disabled
                        ? "Chat is unavailable until the pipeline is ready…"
                        : "Ask about your documents…"
                }
                aria-label="Message"
                className="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none disabled:opacity-50"
            />
            {isStreaming ? (
                <Button
                    type="button"
                    size="icon"
                    variant="secondary"
                    onClick={onCancel}
                    aria-label="Stop"
                    title="Stop"
                >
                    <Square className="size-4" />
                </Button>
            ) : (
                <Button
                    type="submit"
                    size="icon"
                    disabled={disabled || value.trim().length === 0}
                    aria-label="Send"
                    title="Send"
                >
                    <ArrowUp className="size-4" />
                </Button>
            )}
        </form>
    );
}
