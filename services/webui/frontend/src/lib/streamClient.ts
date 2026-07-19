/**
 * Typed fetch-stream client against the Next.js proxy route (`/api/query/stream`).
 *
 * Adapts donna's `streamChat()` pattern: a `fetch` POST with
 * `Accept: text/event-stream` and an `AbortSignal`. Streams are POST, so there
 * is no EventSource — the body is read manually and framed by the browser-free
 * `SSEByteStreamParser`. The client never reaches the orchestrator directly; it
 * only ever talks to the same-origin proxy.
 *
 * Pre-stream HTTP errors (no SSE body — a 404/422/500/502/503 from the proxy)
 * are mapped to a typed `error` StreamEvent so the caller handles success and
 * failure through one honest code path; the `Retry-After` header on a 503 is
 * surfaced as `retry_after_seconds`.
 */

import type { HistoryTurn } from "./history";
import type { StreamError, StreamEvent } from "./envelope";
import { SSEByteStreamParser, classifyEvent } from "./sse";

export const QUERY_STREAM_PATH = "/api/query/stream";

export interface StreamRequest {
    question: string;
    history: HistoryTurn[];
    pipelineConfig: string;
    /**
     * Optional per-request retrieval index scope (project-scoped chat).
     * Composed by `buildRetrievalIndices`; `undefined` OMITS the field so the
     * orchestrator keeps its single-`legal-rag-bench` default exactly.
     */
    retrievalIndices?: string[];
}

/** Build the exact JSON body the orchestrator's QueryRequest expects. */
export function buildRequestBody(request: StreamRequest): string {
    return JSON.stringify({
        question: request.question,
        history: request.history.length > 0 ? request.history : undefined,
        pipeline_config: request.pipelineConfig,
        // Omitted when absent (project-scoped chat rides through the proxy
        // verbatim); absent ⇒ orchestrator default single index, byte-identical.
        retrieval_indices:
            request.retrievalIndices && request.retrievalIndices.length > 0
                ? request.retrievalIndices
                : undefined,
    });
}

function isEventStream(response: Response): boolean {
    const contentType = response.headers.get("content-type") ?? "";
    return contentType.toLowerCase().includes("text/event-stream");
}

async function toHttpError(response: Response): Promise<StreamError> {
    let detail = `Request failed with HTTP ${response.status}.`;
    let dependency: string | undefined;
    let retryAfterSeconds: number | undefined;
    try {
        const data = (await response.clone().json()) as Record<string, unknown>;
        if (typeof data.detail === "string") detail = data.detail;
        if (typeof data.dependency === "string") dependency = data.dependency;
        if (typeof data.retry_after_seconds === "number") {
            retryAfterSeconds = data.retry_after_seconds;
        }
    } catch {
        // Non-JSON body — keep the generic detail.
    }
    if (retryAfterSeconds === undefined) {
        const header = response.headers.get("retry-after");
        if (header) {
            const parsed = Number(header);
            if (!Number.isNaN(parsed)) retryAfterSeconds = parsed;
        }
    }
    return {
        detail,
        dependency,
        retry_after_seconds: retryAfterSeconds,
        http_equivalent: response.status,
    };
}

/**
 * POST the request and drive `onEvent` with each typed SSE event in order.
 * Pre-stream HTTP errors are delivered as a terminal `error` event. Aborts
 * (via `signal`) reject the underlying fetch — the caller owns the controller
 * and applies its own abort semantics, so the rejection is re-thrown.
 */
export async function streamQuery(
    request: StreamRequest,
    onEvent: (event: StreamEvent) => void,
    signal: AbortSignal,
    fetchImpl: typeof fetch = fetch,
): Promise<void> {
    const response = await fetchImpl(QUERY_STREAM_PATH, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            Accept: "text/event-stream",
        },
        body: buildRequestBody(request),
        signal,
    });

    if (!response.ok || !isEventStream(response)) {
        onEvent({ type: "error", error: await toHttpError(response) });
        return;
    }

    const reader = response.body?.getReader();
    if (!reader) {
        onEvent({
            type: "error",
            error: { detail: "The response had no body.", http_equivalent: 502 },
        });
        return;
    }

    const parser = new SSEByteStreamParser();
    const dispatch = (frames: ReturnType<SSEByteStreamParser["push"]>) => {
        for (const frame of frames) {
            const event = classifyEvent(frame);
            if (event !== null) onEvent(event);
        }
    };

    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) dispatch(parser.push(value));
    }
    dispatch(parser.flush());
}
