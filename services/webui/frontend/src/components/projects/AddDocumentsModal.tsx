"use client";

/*
 * Adapted from references/donna/frontend/src/app/components/shared/AddDocumentsModal.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). A markdown + PDF file picker
 * that uploads to the BFF's POST /projects/{id}/documents. Accepted formats
 * mirror the BFF exactly (.md/.markdown/.pdf); PDF is parsed natively by
 * ingestion (pypdf) — DOCX/image/OCR remain out of scope. Donna's
 * standalone-document browser, addDocumentToProject, owner-only delete
 * warnings, and file directory are all out of scope and dropped. Ingestion is
 * SYNCHRONOUS, so the upload BLOCKS — the modal shows an honest blocking state,
 * never a fabricated progress bar.
 */

import { useRef, useState } from "react";
import { Upload, Loader2, X } from "lucide-react";
import { uploadDocument, type BffDocument, type BffError } from "@/lib/bff";
import { SYNCHRONOUS_INGEST_LABEL } from "@/lib/documentStatus";

// Mirrors the BFF's _SUPPORTED_EXTENSIONS (services/webui/backend/app/api/documents.py).
const UPLOAD_ACCEPT = ".md,.markdown,.pdf";

interface Props {
    open: boolean;
    projectId: string;
    onClose: () => void;
    onUploaded: (document: BffDocument) => void;
}

export function AddDocumentsModal({ open, projectId, onClose, onUploaded }: Props) {
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState("");
    const fileInputRef = useRef<HTMLInputElement>(null);

    if (!open) return null;

    function isSupported(file: File): boolean {
        const name = file.name.toLowerCase();
        return (
            name.endsWith(".md") ||
            name.endsWith(".markdown") ||
            name.endsWith(".pdf")
        );
    }

    async function handleUpload(event: React.ChangeEvent<HTMLInputElement>) {
        const files = Array.from(event.target.files ?? []);
        event.target.value = "";
        if (files.length === 0) return;

        const supported = files.filter(isSupported);
        if (supported.length !== files.length) {
            setError("Only markdown (.md/.markdown) and PDF (.pdf) files are accepted.");
            if (supported.length === 0) return;
        }

        setUploading(true);
        setError("");
        try {
            for (const file of supported) {
                const document = await uploadDocument(projectId, file);
                onUploaded(document);
            }
            onClose();
        } catch (err) {
            setError((err as BffError).detail || "Upload failed");
        } finally {
            setUploading(false);
        }
    }

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-4 backdrop-blur-xs">
            <div className="w-full max-w-md rounded-2xl bg-card text-card-foreground shadow-2xl">
                <div className="flex items-center justify-between px-6 pt-5 pb-2">
                    <h2 className="text-sm font-medium text-foreground">
                        Upload a document
                    </h2>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close"
                        className="rounded-lg p-1.5 text-muted-foreground hover:bg-accent"
                    >
                        <X className="size-4" />
                    </button>
                </div>

                <div className="px-6 pb-6">
                    <p className="text-xs text-muted-foreground">
                        The file is stored and ingested synchronously — this
                        blocks until ingestion finishes and reports the real
                        result (indexed, dedup-skipped, or failed).
                    </p>

                    <input
                        ref={fileInputRef}
                        type="file"
                        accept={UPLOAD_ACCEPT}
                        multiple
                        className="hidden"
                        onChange={handleUpload}
                    />
                    <button
                        type="button"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={uploading}
                        className="mt-4 flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-border py-8 text-sm text-muted-foreground hover:bg-accent disabled:opacity-60"
                    >
                        {uploading ? (
                            <Loader2 className="size-4 animate-spin" />
                        ) : (
                            <Upload className="size-4" />
                        )}
                        {uploading ? SYNCHRONOUS_INGEST_LABEL : "Choose a .md or .pdf file"}
                    </button>

                    {error && <p className="mt-3 text-sm text-destructive">{error}</p>}
                </div>
            </div>
        </div>
    );
}
