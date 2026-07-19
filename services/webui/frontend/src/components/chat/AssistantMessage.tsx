"use client";

import { AlertTriangle, Loader2 } from "lucide-react";
import type { AssistantMessageData } from "@/lib/chatStore";
import { numberCitations, sourceCardId } from "@/lib/citations";
import type { RawCitation, RetrievedChunk } from "@/lib/envelope";
import { isWaitingOnBufferedGeneration, retryGuidance, type FinalRender } from "@/lib/render";
import { AnswerMarkdown } from "./AnswerMarkdown";
import { GuardrailChip } from "./GuardrailChip";

/**
 * Honest assistant render. Both streaming branches are first-class:
 *   - live-token branch: real deltas accumulate as plain streamed text;
 *   - buffered-final branch: an honest "waiting on guarded generation" state
 *     (zero tokens) then the full answer rendered at once from `final`.
 * Citations / numbered source cards / timings / provenance / the guardrail chip
 * render ONLY from the `final` envelope. On error/abort the partial text stays
 * visible marked INCOMPLETE.
 */
export function AssistantMessage({ data }: { data: AssistantMessageData }) {
    const { turn, text } = data;

    if (turn.phase === "final" && turn.final) {
        return <FinalAnswer final={turn.final} />;
    }

    return (
        <div className="space-y-2">
            {turn.phase === "streaming" && isWaitingOnBufferedGeneration(turn) && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Loader2 className="size-4 animate-spin" aria-hidden />
                    Waiting on guarded generation…
                </div>
            )}

            {text.length > 0 && (
                <div className="answer-prose whitespace-pre-wrap text-sm leading-relaxed text-foreground">
                    {text}
                    {turn.phase === "streaming" && (
                        <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-foreground/50 align-middle" />
                    )}
                </div>
            )}

            {turn.incomplete && (
                <span className="inline-flex items-center gap-1.5 rounded-full border border-destructive/40 bg-destructive/10 px-2.5 py-1 text-xs font-medium text-destructive">
                    <AlertTriangle className="size-3.5" aria-hidden />
                    INCOMPLETE — this turn was dropped from history
                </span>
            )}

            {turn.phase === "aborted" && (
                <p className="text-xs text-muted-foreground">
                    Stopped by you. Generation was cancelled upstream.
                </p>
            )}

            {turn.phase === "error" && turn.error && (
                <div className="space-y-1 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
                    <p className="font-medium text-destructive">
                        The stream failed before completing.
                    </p>
                    <p>Detail: {turn.error.detail}</p>
                    {turn.error.dependency && <p>Dependency: {turn.error.dependency}</p>}
                    {turn.error.httpEquivalent !== undefined && (
                        <p>HTTP equivalent: {turn.error.httpEquivalent}</p>
                    )}
                    <p className="text-muted-foreground">{retryGuidance(turn.error)}</p>
                </div>
            )}
        </div>
    );
}

function FinalAnswer({ final }: { final: FinalRender }) {
    const rawCitations: RawCitation[] = final.citations.map((c) => ({
        chunk_ids: c.chunkIds,
        claim_span: [c.start, c.end],
    }));
    const chunks: RetrievedChunk[] = final.sources.map((s) => ({
        chunk_id: s.chunkId,
        rank: s.rank,
        score: s.score,
        text: s.text,
    }));
    const numbered = numberCitations(final.answerText, rawCitations, chunks);

    return (
        <div className="space-y-4">
            <AnswerMarkdown text={numbered.text} validNumbers={numbered.validNumbers} />

            {final.guardrailDecisions.length > 0 && (
                <GuardrailChip decisions={final.guardrailDecisions} />
            )}

            {numbered.sources.length > 0 ? (
                <details className="group">
                    <summary className="cursor-pointer select-none text-xs font-semibold uppercase tracking-wide text-muted-foreground marker:text-muted-foreground hover:text-foreground">
                        Cited sources ({numbered.sources.length})
                    </summary>
                    <div className="mt-2 space-y-2">
                        {numbered.sources.map((source) => (
                            <div
                                key={source.number}
                                id={sourceCardId(source.number)}
                                className="rounded-md border border-border bg-card p-3 text-sm"
                            >
                                <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                                    <span className="rounded bg-secondary px-1.5 py-0.5 font-medium text-secondary-foreground">
                                        {source.anchor}
                                    </span>
                                    <span>rank {source.rank}</span>
                                    <span>score {source.score.toFixed(4)}</span>
                                    <span className="truncate">chunk {source.chunkId}</span>
                                </div>
                                <p className="whitespace-pre-wrap text-foreground">{source.text}</p>
                            </div>
                        ))}
                    </div>
                </details>
            ) : (
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    Citations: 0 — the model answered without citing (reported honestly)
                </p>
            )}

            <section className="space-y-1 text-xs text-muted-foreground">
                <h3 className="font-semibold uppercase tracking-wide">Timings (ms)</h3>
                <ul className="flex flex-wrap gap-x-4 gap-y-0.5">
                    {Object.entries(final.timingsMs).map(([stage, ms]) => (
                        <li key={stage}>
                            {stage}: {ms.toFixed(1)}
                        </li>
                    ))}
                </ul>
            </section>

            <section className="space-y-0.5 text-xs text-muted-foreground">
                <h3 className="font-semibold uppercase tracking-wide">Provenance</h3>
                <p>config ref: {final.pipelineVersion}</p>
                <p>config_sha256: {final.configSha256Short}…</p>
                <p>index: {final.index}</p>
            </section>
        </div>
    );
}
