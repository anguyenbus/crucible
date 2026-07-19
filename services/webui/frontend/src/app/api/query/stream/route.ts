/**
 * POST proxy: the browser's ONLY path to the orchestrator's `POST
 * /query/stream`. The orchestrator has no CORS and must not gain any; this
 * route-handler is the topology-proof seam the Phase-2 BFF later absorbs.
 *
 * Hard requirements (each has a test in `route.test.ts`):
 *   (a) abort propagation — the client's `request.signal` is passed straight
 *       into the upstream fetch, so a client abort cancels the upstream stream
 *       (server-side GeneratorExit handling is verified safe); otherwise Stop
 *       is cosmetic and Bedrock keeps generating.
 *   (b) no buffering — the upstream `text/event-stream` ReadableStream is
 *       returned straight to the client, per-event, uncompressed
 *       (`Content-Encoding` dropped, `no-transform`); the envelope is never
 *       collected-then-flushed, rewritten, or enriched.
 *   (c) pre-stream HTTP errors (404/422/500/502/503, `Retry-After` on 503) pass
 *       through as plain HTTP responses — NO SSE body is fabricated.
 */

import { orchestratorUrl } from "@/lib/serverEnv";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Headers that must never be copied from the upstream response: hop-by-hop and
// encoding/length headers that would fight per-event streaming.
const STRIPPED_RESPONSE_HEADERS = new Set([
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
]);

export async function POST(request: Request): Promise<Response> {
    const body = await request.text();

    const upstream = await fetch(`${orchestratorUrl()}/query/stream`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            Accept: "text/event-stream",
        },
        body,
        // (a) Client abort cancels the upstream fetch.
        signal: request.signal,
        // Never let the platform buffer/transform a streaming response.
        cache: "no-store",
    });

    const headers = new Headers();
    upstream.headers.forEach((value, key) => {
        if (!STRIPPED_RESPONSE_HEADERS.has(key.toLowerCase())) {
            headers.set(key, value);
        }
    });
    // (b) Defeat any proxy/CDN buffering and compression of the event stream.
    headers.set("Cache-Control", "no-store, no-transform");
    headers.set("X-Accel-Buffering", "no");

    // (b)/(c): the body — whether a live event-stream or a plain HTTP error body
    // — is passed through byte-verbatim, never rewritten. Pre-stream errors keep
    // their status and their `Retry-After` header (copied above).
    return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers,
    });
}
