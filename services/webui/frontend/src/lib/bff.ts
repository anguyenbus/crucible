/**
 * Typed client for the webui BFF (projects/documents/upload), reached
 * browser-DIRECT via `NEXT_PUBLIC_BFF_URL`.
 *
 * Two-backend topology: chat STAYS on the Phase-1 Next.js proxy to the
 * orchestrator (`/api/query/stream`); this client NEVER touches chat. The BFF
 * is a separate backend with its own CORS, so — unlike the orchestrator — the
 * browser calls it directly. `NEXT_PUBLIC_BFF_URL` is a non-secret address that
 * may reach the client bundle by design.
 *
 * Every function takes an optional `fetchImpl` so the request-building logic is
 * unit-tested browser-free (mirrors `streamClient`/`readyz`).
 */

export type DocumentStatus = "pending" | "indexed" | "skipped" | "failed";

/**
 * The live ingestion step while a document is `pending` (browser polls for it).
 * `queued` is the BFF's own pre-ingest state; the rest mirror the ingestion
 * service's SSE phases. NULL once a terminal status is reached.
 */
export type DocumentPhase =
    | "queued"
    | "parsing"
    | "chunking"
    | "embedding"
    | "indexing";

export interface BffProject {
    id: string;
    name: string;
    /** Per-project OpenSearch index (`proj-{id}`); the chat scope reads it. */
    index_name: string;
    document_count: number;
    created_at: string;
    updated_at: string;
}

export interface BffDocument {
    id: string;
    project_id: string;
    filename: string;
    size_bytes: number;
    storage_uri: string;
    status: DocumentStatus;
    /** Live ingestion progress while `status === "pending"` (NULL when terminal).
     *  `phase_current`/`phase_total` are the embedding chunk counters. */
    phase?: DocumentPhase | null;
    phase_current?: number | null;
    phase_total?: number | null;
    ingest_doc_id: string | null;
    sha256: string | null;
    chunks_indexed: number | null;
    skipped: boolean | null;
    failure_reason: string | null;
    failure_code: number | null;
    created_at: string;
    updated_at: string;
}

export interface BffProjectDetail extends BffProject {
    documents: BffDocument[];
}

/** One indexed chunk (its ordinal + stored text) for the document-detail view. */
export interface BffDocumentChunk {
    /** OpenSearch `_id` / citation key (e.g. `{doc_id}:{chunk_index}`). Optional
     *  so the UI still renders against an ingestion that predates this field. */
    id?: string;
    chunk_index: number;
    content: string;
    /** Per-chunk indexing timestamp (optional; older ingestion omits it). */
    created_at?: string;
}

/**
 * The ACTUAL indexed text for one document (right pane of the detail view).
 * `chunk_count === 0` with empty `text` is an honest "nothing was indexed"
 * state — never fabricated.
 */
export interface BffDocumentText {
    chunk_count: number;
    text: string;
    chunks: BffDocumentChunk[];
    /** Embedding dimension every chunk carries (Titan v2 = 1024). Optional so the
     *  UI still renders against an ingestion that predates this field. */
    embedding_dims?: number;
}

export interface BffError {
    detail: string;
    status: number;
}

export function bffBaseUrl(): string {
    const url = process.env.NEXT_PUBLIC_BFF_URL;
    return url && url.length > 0 ? url.replace(/\/+$/, "") : "http://localhost:8002";
}

async function toBffError(response: Response): Promise<BffError> {
    let detail = `Request failed with HTTP ${response.status}.`;
    try {
        const data = (await response.clone().json()) as Record<string, unknown>;
        if (typeof data.detail === "string") detail = data.detail;
    } catch {
        const text = await response.text().catch(() => "");
        if (text) detail = text;
    }
    return { detail, status: response.status };
}

async function request<T>(
    path: string,
    init: RequestInit,
    fetchImpl: typeof fetch,
): Promise<T> {
    const response = await fetchImpl(`${bffBaseUrl()}${path}`, init);
    if (!response.ok) throw await toBffError(response);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
}

const jsonHeaders = { "Content-Type": "application/json" };

export function listProjects(fetchImpl: typeof fetch = fetch): Promise<BffProject[]> {
    return request<BffProject[]>("/projects", { method: "GET" }, fetchImpl);
}

export function createProject(
    name: string,
    fetchImpl: typeof fetch = fetch,
): Promise<BffProject> {
    return request<BffProject>(
        "/projects",
        { method: "POST", headers: jsonHeaders, body: JSON.stringify({ name }) },
        fetchImpl,
    );
}

export function getProject(
    projectId: string,
    fetchImpl: typeof fetch = fetch,
): Promise<BffProjectDetail> {
    return request<BffProjectDetail>(
        `/projects/${encodeURIComponent(projectId)}`,
        { method: "GET" },
        fetchImpl,
    );
}

export function renameProject(
    projectId: string,
    name: string,
    fetchImpl: typeof fetch = fetch,
): Promise<BffProjectDetail> {
    return request<BffProjectDetail>(
        `/projects/${encodeURIComponent(projectId)}`,
        { method: "PATCH", headers: jsonHeaders, body: JSON.stringify({ name }) },
        fetchImpl,
    );
}

export function deleteProject(
    projectId: string,
    fetchImpl: typeof fetch = fetch,
): Promise<void> {
    return request<void>(
        `/projects/${encodeURIComponent(projectId)}`,
        { method: "DELETE" },
        fetchImpl,
    );
}

export function listDocuments(
    projectId: string,
    fetchImpl: typeof fetch = fetch,
): Promise<BffDocument[]> {
    return request<BffDocument[]>(
        `/projects/${encodeURIComponent(projectId)}/documents`,
        { method: "GET" },
        fetchImpl,
    );
}

/**
 * Upload one markdown/PDF file. Ingestion is ASYNCHRONOUS: the BFF returns a
 * 202 with the accepted document in `pending`/`queued` state and runs the
 * ingest in the background. The caller uploads multiple files by firing several
 * of these concurrently, then POLLS `listDocuments` (watching `status`/`phase`)
 * until each row reaches a terminal status (`indexed` / `skipped` / `failed`).
 */
export function uploadDocument(
    projectId: string,
    file: File,
    fetchImpl: typeof fetch = fetch,
): Promise<BffDocument> {
    const form = new FormData();
    form.append("file", file);
    return request<BffDocument>(
        `/projects/${encodeURIComponent(projectId)}/documents`,
        { method: "POST", body: form },
        fetchImpl,
    );
}

export function deleteDocument(
    projectId: string,
    documentId: string,
    fetchImpl: typeof fetch = fetch,
): Promise<void> {
    return request<void>(
        `/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}`,
        { method: "DELETE" },
        fetchImpl,
    );
}

/**
 * The absolute URL that streams a document's stored bytes back inline (the BFF
 * serves it with `Content-Disposition: inline` and NO `X-Frame-Options`, so an
 * `<object>`/`<iframe>` can embed it). A plain URL — no fetch — because the
 * browser element loads it directly.
 */
export function documentFileUrl(projectId: string, docId: string): string {
    return (
        `${bffBaseUrl()}/projects/${encodeURIComponent(projectId)}` +
        `/documents/${encodeURIComponent(docId)}/file`
    );
}

/**
 * Fetch the ACTUAL indexed text for a document (right pane of the detail view).
 * Surfaces a typed `BffError` on a non-2xx, mirroring the other client calls.
 */
export function getDocumentText(
    projectId: string,
    docId: string,
    fetchImpl: typeof fetch = fetch,
): Promise<BffDocumentText> {
    return request<BffDocumentText>(
        `/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(docId)}/text`,
        { method: "GET" },
        fetchImpl,
    );
}

/**
 * LLM analysis of one document: a plain-English summary plus discrete extracted
 * facts, produced on demand by the orchestrator's non-RAG `/analyze` over the
 * document's indexed text. `truncated` flags that the text was shortened to the
 * analysis cap before the model saw it.
 */
export interface BffDocumentFacts {
    summary: string;
    facts: string[];
    model_id: string;
    truncated: boolean;
}

/** Which analysis to run: `facts` (Extract), `summary` (Summarise), or both. */
export type AnalyzeMode = "facts" | "summary" | "both";

/**
 * Run facts extraction and/or summary for a document (on demand). This BLOCKS on
 * a live LLM generation, so it can take several seconds; a document that never
 * indexed surfaces a typed `BffError` (422), not an empty result. `mode` lets
 * the two buttons ("Extract" / "Summarise") run independently.
 */
export function analyzeDocument(
    projectId: string,
    docId: string,
    mode: AnalyzeMode = "both",
    fetchImpl: typeof fetch = fetch,
): Promise<BffDocumentFacts> {
    return request<BffDocumentFacts>(
        `/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(docId)}` +
            `/analyze?mode=${mode}`,
        { method: "POST" },
        fetchImpl,
    );
}

/** The six contradiction categories (mirrors the BFF/orchestrator taxonomy). */
export type ContradictionType =
    | "temporal"
    | "numerical"
    | "authority"
    | "process"
    | "policy_reversal"
    | "specificity";

/**
 * One classified contradiction between two documents. `quote_a`/`quote_b` are
 * the EXACT conflicting spans from each document (verbatim, never paraphrased).
 */
export interface BffContradiction {
    type: ContradictionType;
    description: string;
    quote_a: string;
    quote_b: string;
}

/**
 * The contradiction report between two documents, produced on demand by the
 * orchestrator's non-RAG `/compare` (decompose-then-verify) over both documents'
 * indexed text. An empty `contradictions` list is an honest "the documents
 * agree" result. `truncated` flags that either document was shortened to the cap.
 */
export interface BffDocumentComparison {
    document_a: string;
    document_b: string;
    contradictions: BffContradiction[];
    model_id: string;
    truncated: boolean;
}

/**
 * Cross-examine two of a project's documents for contradictions (on demand).
 * This BLOCKS on a multi-call LLM pipeline, so it can take a while; comparing a
 * document with itself is a typed `BffError` (400), and a document that never
 * indexed is a 422 — never a fabricated empty result.
 */
export function compareDocuments(
    projectId: string,
    docIdA: string,
    docIdB: string,
    fetchImpl: typeof fetch = fetch,
): Promise<BffDocumentComparison> {
    return request<BffDocumentComparison>(
        `/projects/${encodeURIComponent(projectId)}/compare`,
        {
            method: "POST",
            headers: jsonHeaders,
            body: JSON.stringify({ document_id_a: docIdA, document_id_b: docIdB }),
        },
        fetchImpl,
    );
}
