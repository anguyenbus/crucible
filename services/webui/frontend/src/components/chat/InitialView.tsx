"use client";

/*
 * Adapted from references/donna/frontend/src/app/components/assistant/InitialView.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). Donna's per-user greeting and
 * logo animation are replaced by the "Document Analyser" wordmark welcome.
 */
import { Wordmark } from "@/components/Wordmark";
import { ChatInput } from "./ChatInput";

interface InitialViewProps {
    onSubmit: (text: string) => void;
    onCancel: () => void;
    isStreaming: boolean;
    disabled?: boolean;
}

export function InitialView({ onSubmit, onCancel, isStreaming, disabled }: InitialViewProps) {
    return (
        <div className="flex h-full w-full flex-col px-6">
            <div className="flex flex-1 flex-col items-center justify-center">
                <div className="w-full max-w-2xl">
                    <h1 className="mb-8 text-center text-3xl font-light">
                        <Wordmark className="font-light" />
                    </h1>
                    <ChatInput
                        onSubmit={onSubmit}
                        onCancel={onCancel}
                        isStreaming={isStreaming}
                        disabled={disabled}
                    />
                    <p className="mb-3 py-3 text-center text-xs text-muted-foreground">
                        Answers are grounded in retrieved documents. AI can make mistakes.
                    </p>
                </div>
            </div>
        </div>
    );
}
