import { cn } from "@/lib/utils";

/**
 * Plain text wordmark replacing donna's logo components
 * (donna-logo.tsx / site-logo.tsx) — no logo asset by decision. The product
 * name is exactly "Document Analyser" (British spelling) everywhere it shows.
 */
export function Wordmark({ className }: { className?: string }) {
    return (
        <span
            className={cn(
                "font-semibold tracking-tight text-foreground select-none",
                className,
            )}
        >
            Document Analyser
        </span>
    );
}
