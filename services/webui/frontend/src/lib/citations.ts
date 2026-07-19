/**
 * Envelope-driven inline citation numbering + degradation — the TS mirror of
 * the orchestrator Chainlit contract (`demo_ui/chat_ui/render.py`
 * `number_citations()` + `chat_elements.py`).
 *
 * The mapping is driven STRICTLY by the envelope's `citations` array and the
 * `retrieved_chunks` list — never by free-text guessing. Honesty rules
 * (binding), mirrored exactly:
 *   - For each DISTINCT cited `chunk_id` that is ALSO present in
 *     `retrieved_chunks` (so it has evidence), assign a stable 1-based number by
 *     order of first `[chunk_id]` marker appearance in the answer text; rewrite
 *     every such `[chunk_id]` → `[n]`.
 *   - An uncited marker (a `[chunk_id]` the model emitted but did NOT list in
 *     `citations`) is LEFT UNTOUCHED as plain text — no number, no link.
 *   - A cited `chunk_id` absent from `retrieved_chunks` (contract violation)
 *     gets no number and its marker stays raw — never a crash, never a
 *     fabricated link.
 *   - Zero citations → text unchanged, empty sources.
 *
 * Pure and framework-free so it is unit-tested without a browser.
 */

import type { RawCitation, RetrievedChunk } from "./envelope";

/** A cited chunk assigned a stable per-response number for a clickable `[n]`. */
export interface NumberedSource {
    number: number;
    /** The exact `[n]` string used both in the answer text and as the anchor. */
    anchor: string;
    chunkId: string;
    rank: number;
    score: number;
    text: string;
}

/** Answer text with cited `[chunk_id]` markers rewritten to `[n]`, + sources. */
export interface NumberedAnswer {
    text: string;
    sources: NumberedSource[];
    /** The set of valid citation numbers (used to make only real `[n]` clickable). */
    validNumbers: Set<number>;
}

export function numberCitations(
    answerText: string,
    citations: RawCitation[],
    retrievedChunks: RetrievedChunk[],
): NumberedAnswer {
    const sourceById = new Map<string, RetrievedChunk>();
    for (const chunk of retrievedChunks) sourceById.set(chunk.chunk_id, chunk);

    // Distinct cited chunk_ids that have evidence (present in retrieved_chunks).
    const citedWithEvidence: string[] = [];
    for (const citation of citations) {
        for (const chunkId of citation.chunk_ids) {
            if (sourceById.has(chunkId) && !citedWithEvidence.includes(chunkId)) {
                citedWithEvidence.push(chunkId);
            }
        }
    }

    // Order by first marker appearance; ids whose marker is absent from the text
    // sort last (by citation order) so numbering stays deterministic.
    const firstMarkerKey = (chunkId: string): [number, number] => {
        const found = answerText.indexOf(`[${chunkId}]`);
        const appears = found >= 0 ? 0 : 1;
        return [appears, found >= 0 ? found : citedWithEvidence.indexOf(chunkId)];
    };
    const ordered = [...citedWithEvidence].sort((a, b) => {
        const [aa, ab] = firstMarkerKey(a);
        const [ba, bb] = firstMarkerKey(b);
        return aa !== ba ? aa - ba : ab - bb;
    });

    const sources: NumberedSource[] = [];
    const numberById = new Map<string, number>();
    ordered.forEach((chunkId, i) => {
        const number = i + 1;
        numberById.set(chunkId, number);
        const source = sourceById.get(chunkId)!;
        sources.push({
            number,
            anchor: `[${number}]`,
            chunkId,
            rank: source.rank,
            score: source.score,
            text: source.text,
        });
    });

    // Bracket-delimited literal replace: `[c:1]` cannot match inside `[c:12]`
    // (the closing `]` differs), so replacement order is irrelevant and only
    // cited markers are touched.
    let text = answerText;
    for (const [chunkId, number] of numberById) {
        text = text.split(`[${chunkId}]`).join(`[${number}]`);
    }

    return {
        text,
        sources,
        validNumbers: new Set(numberById.values()),
    };
}

/** One piece of the answer after splitting out clickable citation markers. */
export type CitationSegment =
    | { type: "text"; value: string }
    | { type: "cite"; number: number; anchor: string };

/**
 * Deterministically split a (already-numbered) text into plain-text runs and
 * clickable `[n]` markers — but ONLY for numbers that are real citations
 * (`validNumbers`). A `[7]` that is not a valid citation stays plain text, so
 * unknown/degraded markers never fabricate a link. Each `cite` segment maps
 * deterministically to the source card whose anchor is `[n]`.
 */
export function segmentCitations(
    text: string,
    validNumbers: Set<number>,
): CitationSegment[] {
    const segments: CitationSegment[] = [];
    const re = /\[(\d+)\]/g;
    let lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
        const number = Number(match[1]);
        if (!validNumbers.has(number)) continue; // degrade: leave as plain text
        if (match.index > lastIndex) {
            segments.push({ type: "text", value: text.slice(lastIndex, match.index) });
        }
        segments.push({ type: "cite", number, anchor: `[${number}]` });
        lastIndex = match.index + match[0].length;
    }
    if (lastIndex < text.length) {
        segments.push({ type: "text", value: text.slice(lastIndex) });
    }
    return segments;
}

/** Stable DOM id for a numbered source card (the `[n]` click target). */
export function sourceCardId(number: number): string {
    return `source-card-${number}`;
}
