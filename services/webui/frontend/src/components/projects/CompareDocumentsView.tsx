"use client";

/*
 * Compare two of a project's documents for CONTRADICTIONS. The user selects
 * exactly two indexed documents; on "Compare" the BFF's POST /projects/{id}/compare
 * fetches both documents' indexed text and forwards them to the orchestrator's
 * non-RAG /compare (a LangChain decompose-then-verify pipeline: each document is
 * broken into atomic quoted claims, then the two claim sets are cross-examined).
 * The result is a typed table of contradictions — each classified into one of six
 * categories with the EXACT conflicting quote from each document.
 *
 * Only `indexed` documents are selectable: comparison needs the extracted text,
 * which a pending/failed/skipped document does not have. The call BLOCKS on a
 * multi-step LLM pipeline, so the button shows a spinner while it runs.
 */

import { useMemo, useState } from "react";
import { ArrowLeft, GitCompare, Loader2 } from "lucide-react";
import {
    compareDocuments,
    type BffContradiction,
    type BffDocument,
    type BffDocumentComparison,
    type BffError,
    type ContradictionType,
} from "@/lib/bff";

interface Props {
    projectId: string;
    documents: BffDocument[];
    onBack: () => void;
}

// Human labels for the six contradiction categories (mirrors the taxonomy).
const TYPE_LABELS: Record<ContradictionType, string> = {
    temporal: "Temporal",
    numerical: "Numerical",
    authority: "Authority",
    process: "Process",
    policy_reversal: "Policy Reversal",
    specificity: "Specificity",
};

// A subtle per-type tint for the badge so categories are scannable at a glance.
const TYPE_BADGE: Record<ContradictionType, string> = {
    temporal: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
    numerical: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
    authority: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
    process: "bg-teal-500/10 text-teal-600 dark:text-teal-400",
    policy_reversal: "bg-rose-500/10 text-rose-600 dark:text-rose-400",
    specificity: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
};

function typeLabel(type: ContradictionType): string {
    return TYPE_LABELS[type] ?? type;
}

export function CompareDocumentsView({ projectId, documents, onBack }: Props) {
    const selectable = useMemo(
        () => documents.filter((d) => d.status === "indexed"),
        [documents],
    );
    const notReadyCount = documents.length - selectable.length;

    const [selected, setSelected] = useState<string[]>([]);
    const [result, setResult] = useState<BffDocumentComparison | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    function toggle(id: string) {
        setSelected((prev) => {
            if (prev.includes(id)) return prev.filter((x) => x !== id);
            if (prev.length >= 2) return prev; // cap at two; deselect first to change
            return [...prev, id];
        });
    }

    async function handleCompare() {
        if (selected.length !== 2) return;
        setLoading(true);
        setError(null);
        setResult(null);
        try {
            const comparison = await compareDocuments(projectId, selected[0], selected[1]);
            setResult(comparison);
        } catch (err) {
            setError(
                (err as BffError)?.detail ||
                    "Could not compare the documents. Please try again.",
            );
        } finally {
            setLoading(false);
        }
    }

    function reset() {
        setResult(null);
        setError(null);
        setSelected([]);
    }

    return (
        <div className="flex flex-1 flex-col overflow-hidden">
            <div className="flex items-center gap-3 px-8 py-4">
                <button
                    type="button"
                    onClick={onBack}
                    aria-label="Back to documents"
                    className="rounded-lg p-1.5 text-muted-foreground hover:bg-accent"
                >
                    <ArrowLeft className="size-4" />
                </button>
                <h1 className="flex items-center gap-2 text-2xl font-medium tracking-tight text-foreground">
                    <GitCompare className="size-5 text-muted-foreground" />
                    Compare Documents for Contradiction
                </h1>
            </div>

            <div className="flex-1 overflow-y-auto px-8 py-6">
                {result ? (
                    <ComparisonResult result={result} onReset={reset} />
                ) : (
                    <SelectionPane
                        selectable={selectable}
                        notReadyCount={notReadyCount}
                        selected={selected}
                        loading={loading}
                        error={error}
                        onToggle={toggle}
                        onCompare={handleCompare}
                    />
                )}
            </div>
        </div>
    );
}

function SelectionPane({
    selectable,
    notReadyCount,
    selected,
    loading,
    error,
    onToggle,
    onCompare,
}: {
    selectable: BffDocument[];
    notReadyCount: number;
    selected: string[];
    loading: boolean;
    error: string | null;
    onToggle: (id: string) => void;
    onCompare: () => void;
}) {
    if (selectable.length < 2) {
        return (
            <div className="max-w-xl">
                <p className="py-16 text-center text-sm text-muted-foreground">
                    You need at least two indexed documents to compare.
                    {notReadyCount > 0 &&
                        ` ${notReadyCount} document${
                            notReadyCount === 1 ? " is" : "s are"
                        } still ingesting or unavailable.`}
                </p>
            </div>
        );
    }

    const atCap = selected.length >= 2;

    return (
        <div className="max-w-2xl">
            <p className="mb-4 text-sm text-muted-foreground">
                Select <span className="font-medium text-foreground">two</span> documents,
                then compare them. Each is decomposed into atomic claims and
                cross-examined for genuine contradictions.
            </p>

            <ul className="divide-y divide-border rounded-lg border border-border">
                {selectable.map((document) => {
                    const isSelected = selected.includes(document.id);
                    const disabled = atCap && !isSelected;
                    return (
                        <li key={document.id}>
                            <button
                                type="button"
                                onClick={() => onToggle(document.id)}
                                disabled={disabled}
                                aria-pressed={isSelected}
                                className={`flex w-full items-center gap-3 px-4 py-3 text-left transition-colors ${
                                    isSelected ? "bg-accent/60" : "hover:bg-accent/40"
                                } ${disabled ? "cursor-not-allowed opacity-40" : ""}`}
                            >
                                <span
                                    aria-hidden
                                    className={`flex size-4 shrink-0 items-center justify-center rounded-sm border ${
                                        isSelected
                                            ? "border-primary bg-primary text-primary-foreground"
                                            : "border-border"
                                    }`}
                                >
                                    {isSelected && (
                                        <svg viewBox="0 0 12 12" className="size-3" fill="none">
                                            <path
                                                d="M2.5 6.5l2 2 5-5"
                                                stroke="currentColor"
                                                strokeWidth="1.5"
                                                strokeLinecap="round"
                                                strokeLinejoin="round"
                                            />
                                        </svg>
                                    )}
                                </span>
                                <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                                    {document.filename}
                                </span>
                            </button>
                        </li>
                    );
                })}
            </ul>

            {error && <p className="mt-3 text-sm text-destructive">{error}</p>}

            <div className="mt-4 flex items-center gap-3">
                <button
                    type="button"
                    onClick={onCompare}
                    disabled={selected.length !== 2 || loading}
                    className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                    {loading ? (
                        <Loader2 className="size-4 animate-spin" />
                    ) : (
                        <GitCompare className="size-4" />
                    )}
                    {loading ? "Comparing…" : "Compare"}
                </button>
                <span className="text-xs text-muted-foreground">
                    {selected.length}/2 selected
                </span>
            </div>
            {loading && (
                <p className="mt-3 text-xs text-muted-foreground">
                    Running the decompose-then-verify pipeline — this can take a
                    little while for long documents.
                </p>
            )}
        </div>
    );
}

function ComparisonResult({
    result,
    onReset,
}: {
    result: BffDocumentComparison;
    onReset: () => void;
}) {
    const { contradictions, document_a, document_b, truncated, model_id } = result;

    return (
        <div>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                    <p className="text-sm text-foreground">
                        <span className="font-medium">{document_a}</span>
                        <span className="mx-2 text-muted-foreground">vs</span>
                        <span className="font-medium">{document_b}</span>
                    </p>
                    <p className="text-xs text-muted-foreground">
                        {contradictions.length === 0
                            ? "No contradictions found."
                            : `${contradictions.length} contradiction${
                                  contradictions.length === 1 ? "" : "s"
                              } found.`}
                    </p>
                </div>
                <button
                    type="button"
                    onClick={onReset}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent"
                >
                    Compare other documents
                </button>
            </div>

            {truncated && (
                <p className="mb-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                    One or both documents were long and truncated before analysis, so
                    some contradictions may not be shown.
                </p>
            )}

            {contradictions.length === 0 ? (
                <div className="rounded-lg border border-border bg-card px-4 py-10 text-center">
                    <p className="text-sm text-foreground">
                        These two documents don&apos;t appear to contradict each other.
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                        No temporal, numerical, authority, process, policy, or
                        specificity conflicts were detected.
                    </p>
                </div>
            ) : (
                <div className="overflow-x-auto rounded-lg border border-border">
                    <table className="w-full min-w-[48rem] border-collapse text-sm">
                        <thead>
                            <tr className="border-b border-border bg-muted/40 text-left">
                                <th className="w-32 px-4 py-2.5 font-medium text-muted-foreground">
                                    Type
                                </th>
                                <th className="px-4 py-2.5 font-medium text-muted-foreground">
                                    Description
                                </th>
                                <th className="px-4 py-2.5 font-medium text-muted-foreground">
                                    <span className="block max-w-[16rem] truncate" title={document_a}>
                                        {document_a}
                                    </span>
                                </th>
                                <th className="px-4 py-2.5 font-medium text-muted-foreground">
                                    <span className="block max-w-[16rem] truncate" title={document_b}>
                                        {document_b}
                                    </span>
                                </th>
                            </tr>
                        </thead>
                        <tbody>
                            {contradictions.map((c, i) => (
                                <ContradictionRow key={i} contradiction={c} />
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {model_id && (
                <p className="mt-3 text-xs text-muted-foreground">
                    Analysed by {model_id}.
                </p>
            )}
        </div>
    );
}

function ContradictionRow({ contradiction }: { contradiction: BffContradiction }) {
    const { type, description, quote_a, quote_b } = contradiction;
    return (
        <tr className="border-b border-border align-top last:border-b-0">
            <td className="px-4 py-3">
                <span
                    className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${
                        TYPE_BADGE[type] ?? "bg-muted text-muted-foreground"
                    }`}
                >
                    {typeLabel(type)}
                </span>
            </td>
            <td className="px-4 py-3 text-foreground">{description}</td>
            <td className="px-4 py-3">
                <blockquote className="border-l-2 border-border pl-2 text-xs italic text-muted-foreground">
                    “{quote_a}”
                </blockquote>
            </td>
            <td className="px-4 py-3">
                <blockquote className="border-l-2 border-border pl-2 text-xs italic text-muted-foreground">
                    “{quote_b}”
                </blockquote>
            </td>
        </tr>
    );
}
