import { describe, expect, it } from "vitest";
import { numberCitations, segmentCitations, sourceCardId } from "./citations";
import { citation, makeChunk } from "@/testing/fixtures";

/*
 * Group 4 — the citation mapper, mirroring demo_ui/render.py number_citations().
 * Stable 1-based numbering by first-marker appearance among cited-AND-retrieved
 * chunks; unknown/uncited markers degrade to plain text and never fabricate a
 * link; the [n] anchor maps deterministically to its source card.
 */

describe("numberCitations", () => {
    it("numbers cited+retrieved chunks by first marker appearance and rewrites markers", () => {
        const answer = "Beta says [c:2] while alpha says [c:1] and again [c:2].";
        const numbered = numberCitations(
            answer,
            [citation(["c:2"], [0, 4]), citation(["c:1"], [23, 28])],
            [makeChunk({ chunk_id: "c:1", rank: 3 }), makeChunk({ chunk_id: "c:2", rank: 1 })],
        );

        // c:2 appears first in the text ⇒ [1]; c:1 appears second ⇒ [2].
        expect(numbered.text).toBe("Beta says [1] while alpha says [2] and again [1].");
        expect(numbered.sources.map((s) => s.chunkId)).toEqual(["c:2", "c:1"]);
        expect(numbered.sources[0]).toMatchObject({ number: 1, anchor: "[1]", rank: 1 });
        expect([...numbered.validNumbers].sort()).toEqual([1, 2]);
    });

    it("degrades an uncited marker to plain text (no number, no link)", () => {
        const answer = "Cited [c:1] and uncited [c:9] here.";
        const numbered = numberCitations(
            answer,
            [citation(["c:1"], [0, 5])],
            [makeChunk({ chunk_id: "c:1" }), makeChunk({ chunk_id: "c:9" })],
        );
        // c:9 was retrieved but NOT cited ⇒ its marker is left untouched.
        expect(numbered.text).toBe("Cited [1] and uncited [c:9] here.");
        expect(numbered.sources).toHaveLength(1);
    });

    it("degrades a cited chunk absent from retrieved_chunks (contract violation) gracefully", () => {
        const answer = "Says [c:missing] a thing.";
        const numbered = numberCitations(
            answer,
            [citation(["c:missing"], [0, 4])],
            [makeChunk({ chunk_id: "c:1" })],
        );
        // No evidence ⇒ no number, marker stays raw, no fabricated source.
        expect(numbered.text).toBe("Says [c:missing] a thing.");
        expect(numbered.sources).toHaveLength(0);
        expect(numbered.validNumbers.size).toBe(0);
    });

    it("zero citations returns the text unchanged with empty sources", () => {
        const numbered = numberCitations("Plain answer, no cites.", [], [makeChunk()]);
        expect(numbered.text).toBe("Plain answer, no cites.");
        expect(numbered.sources).toHaveLength(0);
    });

    it("segments a numbered answer, mapping each [n] to its source card id", () => {
        const segments = segmentCitations("See [1] and [2] but not [9].", new Set([1, 2]));
        const cites = segments.filter((s) => s.type === "cite");
        expect(cites).toHaveLength(2);
        expect(sourceCardId(1)).toBe("source-card-1");
        // [9] is not a valid citation number ⇒ stays plain text.
        const text = segments
            .filter((s) => s.type === "text")
            .map((s) => (s.type === "text" ? s.value : ""))
            .join("");
        expect(text).toContain("[9]");
    });
});
