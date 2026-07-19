"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, GitCompare, Plus, Trash2 } from "lucide-react";
import {
    deleteDocument,
    getProject,
    type BffDocument,
    type BffError,
    type BffProjectDetail,
} from "@/lib/bff";
import { describeStatus, isInProgress } from "@/lib/documentStatus";
import { DocumentStatusBadge } from "./DocumentStatusBadge";
import { AddDocumentsModal } from "./AddDocumentsModal";
import { CompareDocumentsView } from "./CompareDocumentsView";
import { DocumentDetailView } from "./DocumentDetailView";
import { ProjectChat } from "./ProjectChat";

interface Props {
    projectId: string;
    projectName: string;
    onBack: () => void;
    onDocumentCountChange: (projectId: string, count: number) => void;
}

type Tab = "info" | "documents" | "chat";

// How often to poll the document list while any upload is still ingesting.
// Phases (parsing → chunking → embedding → indexing) advance on this cadence.
const POLL_INTERVAL_MS = 1500;

export function ProjectDetailPanel({
    projectId,
    projectName,
    onBack,
    onDocumentCountChange,
}: Props) {
    const [detail, setDetail] = useState<BffProjectDetail | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [addOpen, setAddOpen] = useState(false);
    const [selectedDocument, setSelectedDocument] = useState<BffDocument | null>(null);
    const [comparing, setComparing] = useState(false);
    const [tab, setTab] = useState<Tab>("info");
    const [actionError, setActionError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const data = await getProject(projectId);
            setDetail(data);
            onDocumentCountChange(projectId, data.document_count);
        } catch {
            setError("Could not load this project from the BFF.");
        } finally {
            setLoading(false);
        }
    }, [projectId, onDocumentCountChange]);

    useEffect(() => {
        void load();
    }, [load]);

    // Silent re-fetch (no "Loading…" flash) used by the progress poller. A
    // transient failure is swallowed — the next tick retries and the initial
    // `load` owns the hard-error UI.
    const refresh = useCallback(async () => {
        try {
            const data = await getProject(projectId);
            setDetail(data);
            onDocumentCountChange(projectId, data.document_count);
        } catch {
            /* keep last known state; next poll retries */
        }
    }, [projectId, onDocumentCountChange]);

    // Poll the document list ONLY while an upload is mid-ingest; the interval is
    // torn down as soon as every document reaches a terminal status (or unmount).
    const hasPending = (detail?.documents ?? []).some(isInProgress);
    useEffect(() => {
        if (!hasPending) return;
        const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
        return () => clearInterval(timer);
    }, [hasPending, refresh]);

    function handleUploaded(document: BffDocument) {
        setDetail((prev) =>
            prev
                ? {
                      ...prev,
                      documents: [document, ...prev.documents],
                      document_count: prev.document_count + 1,
                  }
                : prev,
        );
        onDocumentCountChange(projectId, (detail?.document_count ?? 0) + 1);
    }

    async function handleDelete(documentId: string) {
        setActionError(null);
        try {
            await deleteDocument(projectId, documentId);
            setDetail((prev) =>
                prev
                    ? {
                          ...prev,
                          documents: prev.documents.filter((d) => d.id !== documentId),
                          document_count: Math.max(0, prev.document_count - 1),
                      }
                    : prev,
            );
        } catch (err) {
            // The delete is gated on index cleanup: on failure the document is
            // kept (still retrievable), so keep it in the list and say why.
            setActionError(
                (err as BffError)?.detail ||
                    "Could not delete the document — its chunks may still be in the index. Try again.",
            );
        }
    }

    const documents = detail?.documents ?? [];

    if (selectedDocument) {
        return (
            <DocumentDetailView
                projectId={projectId}
                document={selectedDocument}
                onBack={() => setSelectedDocument(null)}
            />
        );
    }

    if (comparing) {
        return (
            <CompareDocumentsView
                projectId={projectId}
                documents={documents}
                onBack={() => setComparing(false)}
            />
        );
    }

    return (
        <div className="flex flex-1 flex-col overflow-hidden">
            <div className="flex items-center gap-3 px-8 py-4">
                <button
                    type="button"
                    onClick={onBack}
                    aria-label="Back to projects"
                    className="rounded-lg p-1.5 text-muted-foreground hover:bg-accent"
                >
                    <ArrowLeft className="size-4" />
                </button>
                <h1 className="text-2xl font-medium tracking-tight text-foreground">
                    {detail?.name ?? projectName}
                </h1>
            </div>

            {/* Tabs */}
            <div className="flex gap-1 border-b border-border px-8">
                {(
                    [
                        ["info", "Project info"],
                        ["documents", "Documents"],
                        ["chat", "Chat"],
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
                        {key === "documents" && detail ? ` (${detail.document_count})` : ""}
                    </button>
                ))}
            </div>

            {loading ? (
                <p className="py-16 text-center text-sm text-muted-foreground">Loading…</p>
            ) : error ? (
                <p className="py-16 text-center text-sm text-destructive">{error}</p>
            ) : tab === "chat" && detail ? (
                <ProjectChat
                    projectId={projectId}
                    projectName={detail.name}
                    indexName={detail.index_name}
                />
            ) : tab === "info" ? (
                <div className="flex-1 overflow-y-auto px-8 py-6">
                    <ProjectInfoTab detail={detail} documents={documents} />
                </div>
            ) : (
                <div className="flex-1 overflow-y-auto px-8 py-6">
                    <DocumentsTab
                        documents={documents}
                        actionError={actionError}
                        onUploadClick={() => setAddOpen(true)}
                        onCompareClick={() => setComparing(true)}
                        onSelect={setSelectedDocument}
                        onDelete={handleDelete}
                    />
                </div>
            )}

            <AddDocumentsModal
                open={addOpen}
                projectId={projectId}
                onClose={() => setAddOpen(false)}
                onUploaded={handleUploaded}
            />
        </div>
    );
}

function ProjectInfoTab({
    detail,
    documents,
}: {
    detail: BffProjectDetail | null;
    documents: BffDocument[];
}) {
    if (!detail) return null;

    const byStatus = documents.reduce<Record<string, number>>((acc, d) => {
        acc[d.status] = (acc[d.status] ?? 0) + 1;
        return acc;
    }, {});
    const totalChunks = documents.reduce((sum, d) => sum + (d.chunks_indexed ?? 0), 0);
    const created = new Date(detail.created_at).toLocaleString();

    const rows: [string, string][] = [
        ["Documents", String(detail.document_count)],
        ["Indexed", String(byStatus.indexed ?? 0)],
        ["Dedup-skipped", String(byStatus.skipped ?? 0)],
        ["Failed", String(byStatus.failed ?? 0)],
        ["Total chunks indexed", String(totalChunks)],
        ["Created", created],
        ["Index", detail.index_name],
    ];

    return (
        <div className="max-w-xl">
            <h2 className="mb-1 text-sm font-medium text-foreground">Summary</h2>
            <p className="mb-4 text-sm text-muted-foreground">
                {detail.document_count === 0
                    ? "No documents yet. Add documents in the Documents tab to build this project's searchable index."
                    : `${detail.document_count} document${
                          detail.document_count === 1 ? "" : "s"
                      } · ${totalChunks} chunk${totalChunks === 1 ? "" : "s"} indexed into ${
                          detail.index_name
                      }.`}
            </p>
            <dl className="divide-y divide-border rounded-lg border border-border">
                {rows.map(([label, value]) => (
                    <div key={label} className="flex items-center justify-between gap-4 px-4 py-2.5">
                        <dt className="text-sm text-muted-foreground">{label}</dt>
                        <dd className="max-w-[60%] truncate text-sm text-foreground" title={value}>
                            {value}
                        </dd>
                    </div>
                ))}
            </dl>
        </div>
    );
}

function DocumentsTab({
    documents,
    actionError,
    onUploadClick,
    onCompareClick,
    onSelect,
    onDelete,
}: {
    documents: BffDocument[];
    actionError: string | null;
    onUploadClick: () => void;
    onCompareClick: () => void;
    onSelect: (document: BffDocument) => void;
    onDelete: (documentId: string) => void;
}) {
    // Contradiction comparison needs two documents with extracted text.
    const indexedCount = documents.filter((d) => d.status === "indexed").length;
    return (
        <div>
            {actionError && (
                <p className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    {actionError}
                </p>
            )}
            <div className="mb-4 flex justify-end gap-2">
                <button
                    type="button"
                    onClick={onCompareClick}
                    disabled={indexedCount < 2}
                    title={
                        indexedCount < 2
                            ? "Index at least two documents to compare them"
                            : undefined
                    }
                    className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                    <GitCompare className="size-4" />
                    Compare Documents for Contradiction
                </button>
                <button
                    type="button"
                    onClick={onUploadClick}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                >
                    <Plus className="size-4" />
                    Upload document
                </button>
            </div>

            {documents.length === 0 ? (
                <p className="py-16 text-center text-sm text-muted-foreground">
                    No documents yet. Upload a markdown or PDF file to ingest it.
                </p>
            ) : (
                <ul className="divide-y divide-border rounded-lg border border-border">
                    {documents.map((document) => {
                        const view = describeStatus(document);
                        return (
                            <li
                                key={document.id}
                                className="flex items-center justify-between gap-4"
                            >
                                <button
                                    type="button"
                                    onClick={() => onSelect(document)}
                                    className="flex min-w-0 flex-1 flex-col items-start px-4 py-3 text-left hover:bg-accent/50"
                                >
                                    <span className="max-w-full truncate text-sm text-foreground">
                                        {document.filename}
                                    </span>
                                    {view.detail && (
                                        <span className="max-w-full truncate text-xs text-muted-foreground">
                                            {view.detail}
                                        </span>
                                    )}
                                </button>
                                <div className="flex shrink-0 items-center gap-3 pr-4">
                                    <DocumentStatusBadge document={document} />
                                    <button
                                        type="button"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            void onDelete(document.id);
                                        }}
                                        aria-label={`Delete ${document.filename}`}
                                        className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-destructive"
                                    >
                                        <Trash2 className="size-4" />
                                    </button>
                                </div>
                            </li>
                        );
                    })}
                </ul>
            )}
        </div>
    );
}
