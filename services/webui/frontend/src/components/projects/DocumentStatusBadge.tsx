import { cn } from "@/lib/utils";
import { describeStatus, type StatusTone } from "@/lib/documentStatus";
import type { BffDocument } from "@/lib/bff";

/*
 * Honest per-document ingestion status. Colour is the only decoration; the label
 * and detail come straight from real BFF fields (live phase / chunks indexed /
 * dedup skip / failure reason). A pending row shows the current ingestion phase
 * (e.g. "Embedding…") with a pulsing dot — the phase is the service's own,
 * polled from the BFF, never a fabricated progress bar.
 */

const TONE_CLASSES: Record<StatusTone, string> = {
    indexed: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
    skipped: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
    failed: "bg-destructive/10 text-destructive",
    pending: "bg-muted text-muted-foreground",
};

export function DocumentStatusBadge({ document }: { document: BffDocument }) {
    const view = describeStatus(document);
    return (
        <span
            className={cn(
                "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium",
                TONE_CLASSES[view.tone],
            )}
            title={view.detail ?? undefined}
        >
            {view.blocking && (
                <span className="size-1.5 animate-pulse rounded-full bg-current" />
            )}
            {view.label}
        </span>
    );
}
