/**
 * Honest per-document status mapping (real BFF data only — no mocked mode, no
 * fake progress).
 *
 * Ingestion is ASYNCHRONOUS: an in-flight upload advances through real phases
 * (queued → parsing → chunking → embedding i/N → indexing) that the BFF records
 * from the ingestion service's SSE and the browser polls. `describeStatus`
 * turns a document into the descriptor the list renders — the phases are the
 * service's own, never fabricated. The mapping is unit-tested browser-free.
 */

import type { BffDocument, DocumentPhase, DocumentStatus } from "./bff";

export type StatusTone = "pending" | "indexed" | "skipped" | "failed";

export interface StatusView {
    tone: StatusTone;
    label: string;
    /** Secondary line (live phase / chunk count / dedup note / failure reason). */
    detail: string | null;
    /** True while ingestion is still in flight (drives the pulsing indicator). */
    blocking: boolean;
}

/** Fallback in-progress copy when a pending doc has no phase yet (older BFF). */
export const SYNCHRONOUS_INGEST_LABEL = "Uploading and indexing…";

/** Short label for a known live phase, shown as the badge text while pending. */
const PHASE_LABELS: Record<DocumentPhase, string> = {
    queued: "Queued",
    parsing: "Parsing",
    chunking: "Chunking",
    embedding: "Embedding",
    indexing: "Indexing",
};

/**
 * Human label for a phase. Known phases use their curated label; an UNKNOWN
 * phase (e.g. one a newer, independently-deployed ingestion emits before this
 * UI learns it) is humanized rather than shown as "undefined" — forward-compat.
 */
function phaseLabel(phase: string): string {
    return (
        PHASE_LABELS[phase as DocumentPhase] ??
        phase.charAt(0).toUpperCase() + phase.slice(1)
    );
}

/** The verbose secondary line for a live phase (embedding shows i/N). */
function describePhase(document: BffDocument): string {
    const phase = document.phase ?? undefined;
    if (phase === undefined) return SYNCHRONOUS_INGEST_LABEL;
    if (
        phase === "embedding" &&
        typeof document.phase_current === "number" &&
        typeof document.phase_total === "number" &&
        document.phase_total > 0
    ) {
        return `Embedding chunk ${document.phase_current} of ${document.phase_total}…`;
    }
    return `${phaseLabel(phase)}…`;
}

/** The badge text while a doc is pending (the live phase, or a generic fallback). */
function pendingLabel(document: BffDocument): string {
    const phase = document.phase ?? undefined;
    return phase === undefined ? "Ingesting…" : `${phaseLabel(phase)}…`;
}

export function describeStatus(document: BffDocument): StatusView {
    const status: DocumentStatus = document.status;
    switch (status) {
        case "indexed": {
            const count = document.chunks_indexed ?? 0;
            return {
                tone: "indexed",
                label: "Indexed",
                detail: `${count} ${count === 1 ? "chunk" : "chunks"} indexed`,
                blocking: false,
            };
        }
        case "skipped":
            return {
                tone: "skipped",
                label: "Skipped",
                detail: "Already indexed — dedup skip",
                blocking: false,
            };
        case "failed":
            return {
                tone: "failed",
                label: "Failed",
                detail:
                    document.failure_reason ??
                    (document.failure_code
                        ? `Ingestion failed (HTTP ${document.failure_code}).`
                        : "Ingestion failed."),
                blocking: false,
            };
        case "pending":
        default:
            return {
                tone: "pending",
                label: pendingLabel(document),
                detail: describePhase(document),
                blocking: true,
            };
    }
}

/** True if a document is still being ingested (drives whether the list polls). */
export function isInProgress(document: BffDocument): boolean {
    return document.status === "pending";
}
