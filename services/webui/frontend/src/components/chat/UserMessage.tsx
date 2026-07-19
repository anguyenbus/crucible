/*
 * Adapted from references/donna/frontend/src/app/components/assistant/UserMessage.tsx
 * (MIT (c) 2026 Donna Contributors, same owner).
 */
export function UserMessage({ content }: { content: string }) {
    return (
        <div className="flex justify-end">
            <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl bg-secondary px-4 py-2.5 text-sm text-secondary-foreground">
                {content}
            </div>
        </div>
    );
}
