"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, Download, FileText, Sparkles } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
    analyzeDocument,
    documentFileUrl,
    getDocumentText,
    type BffDocument,
    type BffDocumentText,
    type BffError,
} from "@/lib/bff";
import { DocumentStatusBadge } from "./DocumentStatusBadge";

interface Props {
    projectId: string;
    document: BffDocument;
    onBack: () => void;
}

type Kind = "pdf" | "markdown" | "other";

function kindOf(filename: string): Kind {
    const lower = filename.toLowerCase();
    if (lower.endsWith(".pdf")) return "pdf";
    if (lower.endsWith(".md") || lower.endsWith(".markdown")) return "markdown";
    return "other";
}

/**
 * Two-pane document detail: LEFT renders the original file (a native `<object>`
 * for PDFs — no pdfjs/heavy dep — or the fetched markdown; a download fallback
 * otherwise), RIGHT shows the ACTUAL indexed chunk text from the BFF. Both panes
 * are honest: the right pane shows an empty state when nothing was indexed, and
 * the left pane always offers a direct link when it cannot inline the file.
 */
type Tab = "info" | "view" | "chunks" | "facts";

export function DocumentDetailView({ projectId, document, onBack }: Props) {
    const kind = kindOf(document.filename);
    const fileUrl = documentFileUrl(projectId, document.id);
    const [tab, setTab] = useState<Tab>("info");

    // Facts extraction and summary are INDEPENDENT paid LLM runs (the two
    // buttons). State is lifted here so a result survives tab switches (run it,
    // browse away, come back); everything resets when the document changes.
    const [facts, setFacts] = useState<string[] | null>(null);
    const [summary, setSummary] = useState<string | null>(null);
    const [factsState, setFactsState] = useState<AnalysisState>({ loading: false, error: null });
    const [summaryState, setSummaryState] = useState<AnalysisState>({
        loading: false,
        error: null,
    });

    useEffect(() => {
        setFacts(null);
        setSummary(null);
        setFactsState({ loading: false, error: null });
        setSummaryState({ loading: false, error: null });
    }, [document.id]);

    const extractFacts = useCallback(async () => {
        setFactsState({ loading: true, error: null });
        try {
            const result = await analyzeDocument(projectId, document.id, "facts");
            setFacts(result.facts);
            setFactsState({ loading: false, error: null, truncated: result.truncated });
        } catch (err) {
            setFactsState({
                loading: false,
                error: (err as BffError)?.detail || "Could not extract facts. Try again.",
            });
        }
    }, [projectId, document.id]);

    const summarise = useCallback(async () => {
        setSummaryState({ loading: true, error: null });
        try {
            const result = await analyzeDocument(projectId, document.id, "summary");
            setSummary(result.summary);
            setSummaryState({ loading: false, error: null, truncated: result.truncated });
        } catch (err) {
            setSummaryState({
                loading: false,
                error: (err as BffError)?.detail || "Could not summarise. Try again.",
            });
        }
    }, [projectId, document.id]);

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
                <div className="min-w-0 flex-1">
                    <h1 className="truncate text-xl font-medium tracking-tight text-foreground">
                        {document.filename}
                    </h1>
                </div>
                <DocumentStatusBadge document={document} />
            </div>

            {/* Tabs */}
            <div className="flex gap-1 border-b border-border px-8">
                {(
                    [
                        ["info", "Information"],
                        ["view", "View document"],
                        ["chunks", "Chunks"],
                        ["facts", "Facts Extraction"],
                    ] as [Tab, string][]
                ).map(([key, label]) => (
                    <button
                        key={key}
                        type="button"
                        onClick={() => setTab(key)}
                        className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                            tab === key
                                ? "border-primary font-medium text-foreground"
                                : "border-transparent text-muted-foreground hover:text-foreground"
                        }`}
                    >
                        {label}
                    </button>
                ))}
            </div>

            {tab === "info" ? (
                <div className="flex-1 overflow-auto px-8 py-6">
                    <DocumentInfoTab kind={kind} document={document} />
                </div>
            ) : tab === "chunks" ? (
                <div className="flex-1 overflow-auto px-8 py-6">
                    <ChunksTab projectId={projectId} document={document} />
                </div>
            ) : tab === "facts" ? (
                <FactsExtractionTab
                    facts={facts}
                    factsState={factsState}
                    onExtract={extractFacts}
                    summary={summary}
                    summaryState={summaryState}
                    onSummarise={summarise}
                />
            ) : (
                <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden px-8 py-6 lg:grid-cols-2">
                    <section className="flex min-h-[24rem] flex-col overflow-hidden rounded-lg border border-border">
                        <header className="border-b border-border px-4 py-2 text-xs font-medium text-muted-foreground">
                            Document
                        </header>
                        <div className="flex-1 overflow-auto">
                            <SourcePane kind={kind} fileUrl={fileUrl} filename={document.filename} />
                        </div>
                    </section>

                    <ExtractedTextPane projectId={projectId} document={document} />
                </div>
            )}
        </div>
    );
}

function DocumentInfoTab({ kind, document }: { kind: Kind; document: BffDocument }) {
    const typeLabel = kind === "pdf" ? "PDF" : kind === "markdown" ? "Markdown" : "File";
    const sizeKb = (document.size_bytes / 1024).toFixed(1);
    const chunks = document.chunks_indexed ?? 0;
    const created = new Date(document.created_at).toLocaleString();

    const summary =
        document.status === "indexed"
            ? `${typeLabel} · indexed as ${chunks} chunk${chunks === 1 ? "" : "s"} · ${sizeKb} KB`
            : document.status === "skipped"
              ? `${typeLabel} · dedup-skipped (identical content already indexed) · ${sizeKb} KB`
              : document.status === "failed"
                ? `${typeLabel} · ingestion failed${
                      document.failure_reason ? `: ${document.failure_reason}` : ""
                  }`
                : `${typeLabel} · ${document.status}`;

    const rows: [string, string][] = [
        ["Status", document.status],
        ["Chunks from this document", String(chunks)],
        ["Type", typeLabel],
        ["Size", `${sizeKb} KB`],
        ["Created", created],
    ];
    if (document.ingest_doc_id) rows.push(["Ingestion doc id", document.ingest_doc_id]);
    if (document.sha256) rows.push(["SHA-256", document.sha256]);
    if (document.failure_reason) rows.push(["Failure reason", document.failure_reason]);

    return (
        <div className="max-w-xl">
            <h2 className="mb-1 text-sm font-medium text-foreground">Summary</h2>
            <p className="mb-4 text-sm text-muted-foreground">{summary}</p>
            <dl className="divide-y divide-border rounded-lg border border-border">
                {rows.map(([label, value]) => (
                    <div key={label} className="flex items-center justify-between gap-4 px-4 py-2.5">
                        <dt className="shrink-0 text-sm text-muted-foreground">{label}</dt>
                        <dd
                            className="max-w-[60%] truncate font-mono text-xs text-foreground"
                            title={value}
                        >
                            {value}
                        </dd>
                    </div>
                ))}
            </dl>
        </div>
    );
}

interface AnalysisState {
    loading: boolean;
    error: string | null;
    truncated?: boolean;
}

function PaneButton({
    label,
    busyLabel,
    hasResult,
    state,
    onClick,
}: {
    label: string;
    busyLabel: string;
    hasResult: boolean;
    state: AnalysisState;
    onClick: () => void;
}) {
    return (
        <button
            type="button"
            onClick={onClick}
            disabled={state.loading}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
        >
            <Sparkles className="size-3.5" />
            {state.loading ? busyLabel : hasResult ? `Re-${label.toLowerCase()}` : label}
        </button>
    );
}

function FactsExtractionTab({
    facts,
    factsState,
    onExtract,
    summary,
    summaryState,
    onSummarise,
}: {
    facts: string[] | null;
    factsState: AnalysisState;
    onExtract: () => void;
    summary: string | null;
    summaryState: AnalysisState;
    onSummarise: () => void;
}) {
    return (
        <div className="flex flex-1 flex-col overflow-hidden px-8 py-6">
            <p className="mb-4 text-sm text-muted-foreground">
                Extract discrete facts and summarise this document&apos;s indexed text. Each runs
                on demand — use the buttons in each pane.
            </p>

            <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-2">
                {/* Facts pane */}
                <section className="flex min-h-[20rem] flex-col overflow-hidden rounded-lg border border-border">
                    <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-2">
                        <span className="text-xs font-medium text-muted-foreground">
                            Facts extraction
                            {facts ? ` — ${facts.length}` : ""}
                        </span>
                        <PaneButton
                            label="Extract"
                            busyLabel="Extracting…"
                            hasResult={facts !== null}
                            state={factsState}
                            onClick={onExtract}
                        />
                    </header>
                    <div className="flex-1 overflow-auto p-4">
                        {factsState.error && (
                            <p className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                                {factsState.error}
                            </p>
                        )}
                        {factsState.truncated && (
                            <p className="mb-3 text-xs text-muted-foreground">
                                Document was long; only its opening section was analysed.
                            </p>
                        )}
                        {factsState.loading && facts === null ? (
                            <p className="text-sm text-muted-foreground">Extracting facts…</p>
                        ) : facts === null ? (
                            <p className="text-sm text-muted-foreground">
                                No facts yet. Click Extract to pull the key facts from this
                                document.
                            </p>
                        ) : facts.length === 0 ? (
                            <p className="text-sm text-muted-foreground">
                                The model found no discrete facts stated in this document.
                            </p>
                        ) : (
                            <ul className="space-y-2">
                                {facts.map((fact, i) => (
                                    <li
                                        key={i}
                                        className="flex gap-2 text-sm leading-relaxed text-foreground"
                                    >
                                        <span className="mt-0.5 select-none text-xs font-medium text-muted-foreground">
                                            {i + 1}.
                                        </span>
                                        <span>{fact}</span>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </div>
                </section>

                {/* Summary pane */}
                <section className="flex min-h-[20rem] flex-col overflow-hidden rounded-lg border border-border">
                    <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-2">
                        <span className="text-xs font-medium text-muted-foreground">
                            Document summary
                        </span>
                        <PaneButton
                            label="Summarise"
                            busyLabel="Summarising…"
                            hasResult={summary !== null}
                            state={summaryState}
                            onClick={onSummarise}
                        />
                    </header>
                    <div className="flex-1 overflow-auto p-4">
                        {summaryState.error && (
                            <p className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                                {summaryState.error}
                            </p>
                        )}
                        {summaryState.truncated && (
                            <p className="mb-3 text-xs text-muted-foreground">
                                Document was long; only its opening section was summarised.
                            </p>
                        )}
                        {summaryState.loading && summary === null ? (
                            <p className="text-sm text-muted-foreground">Summarising…</p>
                        ) : summary === null ? (
                            <p className="text-sm text-muted-foreground">
                                No summary yet. Click Summarise for a plain-English overview.
                            </p>
                        ) : summary ? (
                            <p className="text-sm leading-relaxed text-foreground">{summary}</p>
                        ) : (
                            <p className="text-sm text-muted-foreground">
                                The model returned no summary for this document.
                            </p>
                        )}
                    </div>
                </section>
            </div>
        </div>
    );
}

function ChunksTab({
    projectId,
    document,
}: {
    projectId: string;
    document: BffDocument;
}) {
    const [data, setData] = useState<BffDocumentText | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [selected, setSelected] = useState(0);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        setSelected(0);
        try {
            setData(await getDocumentText(projectId, document.id));
        } catch {
            setError("Could not load the chunks for this document.");
        } finally {
            setLoading(false);
        }
    }, [projectId, document.id]);

    useEffect(() => {
        void load();
    }, [load]);

    if (loading) {
        return <p className="text-sm text-muted-foreground">Loading chunks…</p>;
    }
    if (error) {
        return <p className="text-sm text-destructive">{error}</p>;
    }
    const chunks = data?.chunks ?? [];
    if (chunks.length === 0) {
        return (
            <p className="text-sm text-muted-foreground">
                No chunks were indexed for this document.
            </p>
        );
    }

    const embeddingDims = data?.embedding_dims;
    const active = chunks[Math.min(selected, chunks.length - 1)];

    return (
        <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
                {chunks.length} chunk{chunks.length === 1 ? "" : "s"} indexed for this document, in
                order. Click a chunk to see its metadata. Each chunk is what retrieval and citations
                operate over.
            </p>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
                {/* Chunk list */}
                <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
                    {chunks.map((chunk, i) => (
                        <li key={chunk.id ?? chunk.chunk_index}>
                            <button
                                type="button"
                                onClick={() => setSelected(i)}
                                className={`flex w-full flex-col items-start gap-1 px-4 py-3 text-left hover:bg-accent/50 ${
                                    i === selected ? "bg-accent" : ""
                                }`}
                            >
                                <span className="text-xs font-medium text-foreground">
                                    Chunk {chunk.chunk_index + 1} / {chunks.length}
                                    <span className="ml-2 font-normal text-muted-foreground">
                                        {chunk.content.length.toLocaleString()} chars
                                    </span>
                                </span>
                                <span className="line-clamp-2 text-xs text-muted-foreground">
                                    {chunk.content.slice(0, 160)}
                                </span>
                            </button>
                        </li>
                    ))}
                </ul>

                {/* Metadata pane for the selected chunk */}
                <aside className="h-fit rounded-lg border border-border">
                    <header className="border-b border-border px-4 py-2 text-xs font-medium text-muted-foreground">
                        Chunk {active.chunk_index + 1} metadata
                    </header>
                    <dl className="divide-y divide-border">
                        {(
                            [
                                ["Index", String(active.chunk_index)],
                                ["Chunk id", active.id ?? "—"],
                                ["Characters", active.content.length.toLocaleString()],
                                ["Embedding", embeddingDims ? `${embeddingDims}-dim vector` : "—"],
                                [
                                    "Indexed",
                                    active.created_at
                                        ? new Date(active.created_at).toLocaleString()
                                        : "—",
                                ],
                                ["Document id", document.ingest_doc_id ?? "—"],
                            ] as [string, string][]
                        ).map(([label, value]) => (
                            <div key={label} className="px-4 py-2">
                                <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">
                                    {label}
                                </dt>
                                <dd
                                    className="mt-0.5 break-words font-mono text-xs text-foreground"
                                    title={value}
                                >
                                    {value}
                                </dd>
                            </div>
                        ))}
                    </dl>
                </aside>
            </div>

            {/* Selected chunk content */}
            <div className="overflow-hidden rounded-lg border border-border">
                <header className="border-b border-border px-4 py-2 text-xs font-medium text-muted-foreground">
                    Chunk {active.chunk_index + 1} content
                </header>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words px-4 py-3 font-mono text-xs leading-relaxed text-foreground">
                    {active.content}
                </pre>
            </div>
        </div>
    );
}

function SourcePane({
    kind,
    fileUrl,
    filename,
}: {
    kind: Kind;
    fileUrl: string;
    filename: string;
}) {
    if (kind === "pdf") {
        return (
            <object data={fileUrl} type="application/pdf" className="h-full min-h-[24rem] w-full">
                <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
                    <FileText className="size-8 text-muted-foreground/50" />
                    <p className="text-sm text-muted-foreground">
                        This browser cannot display the PDF inline.
                    </p>
                    <a
                        href={fileUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                    >
                        Open PDF
                    </a>
                </div>
            </object>
        );
    }
    if (kind === "markdown") {
        return <MarkdownSource fileUrl={fileUrl} />;
    }
    return (
        <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
            <FileText className="size-8 text-muted-foreground/50" />
            <p className="text-sm text-muted-foreground">
                No preview is available for this file type.
            </p>
            <a
                href={fileUrl}
                download={filename}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-accent"
            >
                <Download className="size-4" />
                Download
            </a>
        </div>
    );
}

function MarkdownSource({ fileUrl }: { fileUrl: string }) {
    const [text, setText] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        setText(null);
        setError(null);
        fetch(fileUrl)
            .then((response) => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.text();
            })
            .then((body) => {
                if (!cancelled) setText(body);
            })
            .catch(() => {
                if (!cancelled) setError("Could not load this document.");
            });
        return () => {
            cancelled = true;
        };
    }, [fileUrl]);

    if (error) {
        return <p className="p-4 text-sm text-destructive">{error}</p>;
    }
    if (text === null) {
        return <p className="p-4 text-sm text-muted-foreground">Loading document…</p>;
    }
    return (
        <div className="answer-prose p-4 text-sm leading-relaxed text-foreground">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </div>
    );
}

function ExtractedTextPane({
    projectId,
    document,
}: {
    projectId: string;
    document: BffDocument;
}) {
    const [data, setData] = useState<BffDocumentText | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            setData(await getDocumentText(projectId, document.id));
        } catch {
            setError("Could not load the extracted text.");
        } finally {
            setLoading(false);
        }
    }, [projectId, document.id]);

    useEffect(() => {
        void load();
    }, [load]);

    const chunkCount = data?.chunk_count ?? 0;

    return (
        <section className="flex min-h-[24rem] flex-col overflow-hidden rounded-lg border border-border">
            <header className="border-b border-border px-4 py-2 text-xs font-medium text-muted-foreground">
                Extracted text
                {data ? ` — ${chunkCount} ${chunkCount === 1 ? "chunk" : "chunks"}` : ""}
            </header>
            <div className="flex-1 overflow-auto">
                {loading ? (
                    <p className="p-4 text-sm text-muted-foreground">Loading extracted text…</p>
                ) : error ? (
                    <p className="p-4 text-sm text-destructive">{error}</p>
                ) : chunkCount === 0 ? (
                    <p className="p-4 text-sm text-muted-foreground">
                        No extractable text was indexed for this document.
                    </p>
                ) : (
                    <pre className="whitespace-pre-wrap break-words p-4 font-mono text-xs leading-relaxed text-foreground">
                        {data?.text}
                    </pre>
                )}
            </div>
        </section>
    );
}
