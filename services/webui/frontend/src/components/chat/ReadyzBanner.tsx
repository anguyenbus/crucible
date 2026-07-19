"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Top banner shown when `/readyz` fails: the REAL dependency error text plus a
 * manual retry (auto-retry runs in the background). Input is disabled while
 * this shows, but the session is NOT killed — recovery clears it in place.
 */
export function ReadyzBanner({
    errorText,
    checking,
    onRetry,
}: {
    errorText: string;
    checking: boolean;
    onRetry: () => void;
}) {
    return (
        <div className="flex items-center gap-3 border-b border-destructive/40 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
            <AlertTriangle className="size-4 shrink-0" aria-hidden />
            <div className="flex-1">
                <span className="font-medium">Pipeline not ready. </span>
                <span className="text-destructive/90">{errorText}</span>
            </div>
            <Button
                size="sm"
                variant="outline"
                onClick={onRetry}
                disabled={checking}
                className="gap-1.5"
            >
                <RefreshCw className={checking ? "size-3.5 animate-spin" : "size-3.5"} />
                Retry
            </Button>
        </div>
    );
}
