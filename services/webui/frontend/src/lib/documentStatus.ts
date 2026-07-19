/**
 * Honest per-document status mapping (real BFF data only — no mocked mode, no
 * fake progress).
 *
 * Ingestion is SYNCHRONOUS, so an in-flight upload shows a blocking state, not a
 * fabricated progress bar. `describeStatus` returns a plain descriptor the
 * document list renders; the mapping logic is unit-tested browser-free.
 */

import type { BffDocument, DocumentStatus } from "./bff";

export type StatusTone = "pending" | "indexed" | "skipped" | "failed";

export interface StatusView {
    tone: StatusTone;
    label: string;
    /** Secondary line (chunk count / dedup note / failure reason). */
    detail: string | null;
    /** True only while the synchronous ingest is unresolved (honest blocking). */
    blocking: boolean;
}

/** Copy shown while an upload blocks on the synchronous ingest (no % progress). */
export const SYNCHRONOUS_INGEST_LABEL =
    "Uploading and indexing… this blocks until ingestion finishes.";

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
                label: "Ingesting…",
                detail: SYNCHRONOUS_INGEST_LABEL,
                blocking: true,
            };
    }
}
