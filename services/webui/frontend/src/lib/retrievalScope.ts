/**
 * Project-scoped chat: the pure scope → `retrieval_indices` mapping.
 *
 * Browser-free plain TS (unit-tested in `retrievalScope.test.ts`) so the one
 * rule that decides what index scope a chat turn queries is verifiable without
 * a DOM. The result rides in the request body the browser POSTs to the Next
 * proxy, which forwards it verbatim to the orchestrator's optional
 * `retrieval_indices` field.
 *
 * Rules (spec req 7):
 *   - No project selected (general chat)  → `undefined` (OMIT the field), so
 *     the orchestrator keeps its single-`legal-rag-bench` default exactly.
 *   - A project selected, toggle OFF       → `[projectIndex]` (project docs only).
 *   - A project selected, toggle ON        → `[projectIndex, <general>]`, where
 *     `<general>` comes from the `NEXT_PUBLIC_GENERAL_INDEX` env pin (never a
 *     component literal).
 */

import { getGeneralIndex } from "./config";

export interface ChatScope {
    /** The selected project's `index_name` (`proj-{id}`), or null for general chat. */
    projectIndex: string | null;
    /** The general-index toggle state (default OFF). */
    includeGeneral: boolean;
}

/**
 * Compose the `retrieval_indices` request field from the chat scope.
 * Returns `undefined` when the field must be OMITTED (no project selected).
 */
export function buildRetrievalIndices(scope: ChatScope): string[] | undefined {
    if (!scope.projectIndex) {
        // General chat: omit the field entirely (single-legal-rag-bench default).
        return undefined;
    }
    if (scope.includeGeneral) {
        return [scope.projectIndex, getGeneralIndex()];
    }
    return [scope.projectIndex];
}
