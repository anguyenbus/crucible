/**
 * Task Group 7: the scope → `retrieval_indices` mapping (browser-free).
 *
 * Focused tests (per task 7.1): toggle OFF → `[proj-id]`; toggle ON →
 * `[proj-id, <general>]` from the `NEXT_PUBLIC_GENERAL_INDEX` env pin; no
 * project selected → the field is OMITTED. Exhaustive UI-state permutations are
 * skipped (the mapping is plain TS).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { buildRetrievalIndices } from "./retrievalScope";

describe("buildRetrievalIndices", () => {
    afterEach(() => {
        vi.unstubAllEnvs();
    });

    it("toggle OFF → project index only", () => {
        expect(
            buildRetrievalIndices({ projectIndex: "proj-abc123", includeGeneral: false }),
        ).toEqual(["proj-abc123"]);
    });

    it("toggle ON → project index + the general index (default legal-rag-bench)", () => {
        expect(
            buildRetrievalIndices({ projectIndex: "proj-abc123", includeGeneral: true }),
        ).toEqual(["proj-abc123", "legal-rag-bench"]);
    });

    it("toggle ON uses the NEXT_PUBLIC_GENERAL_INDEX env pin, not a literal", () => {
        vi.stubEnv("NEXT_PUBLIC_GENERAL_INDEX", "custom-general");
        expect(
            buildRetrievalIndices({ projectIndex: "proj-abc123", includeGeneral: true }),
        ).toEqual(["proj-abc123", "custom-general"]);
    });

    it("no project selected → field OMITTED (undefined), preserving today's default", () => {
        expect(
            buildRetrievalIndices({ projectIndex: null, includeGeneral: false }),
        ).toBeUndefined();
        // The toggle is irrelevant with no project: still omitted.
        expect(
            buildRetrievalIndices({ projectIndex: null, includeGeneral: true }),
        ).toBeUndefined();
    });
});
