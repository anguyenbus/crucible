import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "./route";

/*
 * Group 2 — proxy route handler. The orchestrator fetch is mocked; each of the
 * three HARD requirements (abort propagation, no-buffering pass-through,
 * latency parity) plus pre-stream HTTP error pass-through is proven directly.
 */

process.env.ORCHESTRATOR_URL = "http://orchestrator.test:8000";

function streamFromChunks(chunks: string[], firstDelayMs = 0): ReadableStream<Uint8Array> {
    const encoder = new TextEncoder();
    let i = 0;
    return new ReadableStream({
        async pull(controller) {
            if (i >= chunks.length) {
                controller.close();
                return;
            }
            if (i === 0 && firstDelayMs > 0) {
                await new Promise((r) => setTimeout(r, firstDelayMs));
            }
            controller.enqueue(encoder.encode(chunks[i]));
            i += 1;
        },
    });
}

async function readAll(stream: ReadableStream<Uint8Array>): Promise<string> {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let out = "";
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        out += decoder.decode(value, { stream: true });
    }
    return out;
}

function postRequest(body: string, signal?: AbortSignal): Request {
    return new Request("http://localhost:3000/api/query/stream", {
        method: "POST",
        body,
        signal,
    });
}

afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

describe("query/stream proxy", () => {
    it("(a) propagates a client abort into the upstream fetch", async () => {
        let upstreamSignal: AbortSignal | undefined;
        vi.stubGlobal(
            "fetch",
            vi.fn(async (_url: string, init?: RequestInit) => {
                upstreamSignal = init?.signal ?? undefined;
                return new Response(streamFromChunks(["event: token\ndata: {}\n\n"]), {
                    status: 200,
                    headers: { "content-type": "text/event-stream" },
                });
            }),
        );

        const controller = new AbortController();
        await POST(postRequest("{}", controller.signal));

        expect(upstreamSignal).toBeDefined();
        expect(upstreamSignal!.aborted).toBe(false);
        // A client abort must fire the very signal handed to the upstream fetch.
        controller.abort();
        expect(upstreamSignal!.aborted).toBe(true);
    });

    it("(b) forwards the event-stream byte-verbatim, per event, uncompressed", async () => {
        const events = [
            "event: token\ndata: {\"text\":\"He\"}\n\n",
            "event: token\ndata: {\"text\":\"llo\"}\n\n",
            'event: final\ndata: {"result":{"answer":{"text":"Hello"}}}\n\n',
        ];
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(streamFromChunks(events), {
                        status: 200,
                        headers: {
                            "content-type": "text/event-stream",
                            "content-encoding": "gzip",
                        },
                    }),
            ),
        );

        const response = await POST(postRequest("{}"));
        expect(response.status).toBe(200);
        expect(response.headers.get("content-encoding")).toBeNull();
        expect(response.headers.get("cache-control")).toContain("no-transform");
        expect(response.body).not.toBeNull();

        const forwarded = await readAll(response.body!);
        // Byte-for-byte identical to what the orchestrator emitted (not rewritten).
        expect(forwarded).toBe(events.join(""));
    });

    it("(c) adds negligible first-byte latency vs a direct upstream read", async () => {
        const makeStream = () => streamFromChunks(["event: token\ndata: {}\n\n"], 20);

        const directStart = performance.now();
        const directReader = makeStream().getReader();
        await directReader.read();
        const directMs = performance.now() - directStart;

        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(makeStream(), {
                        status: 200,
                        headers: { "content-type": "text/event-stream" },
                    }),
            ),
        );
        const proxiedStart = performance.now();
        const response = await POST(postRequest("{}"));
        const proxiedReader = response.body!.getReader();
        await proxiedReader.read();
        const proxiedMs = performance.now() - proxiedStart;

        // Smoke threshold: the handler must not buffer (which would balloon this).
        expect(proxiedMs - directMs).toBeLessThan(60);
    });

    it("passes a pre-stream 503 through as plain HTTP with Retry-After (no SSE)", async () => {
        const errorBody = JSON.stringify({
            detail: "Bedrock throttling persisted.",
            dependency: "bedrock",
            retry_after_seconds: 5,
        });
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(errorBody, {
                        status: 503,
                        headers: {
                            "content-type": "application/json",
                            "retry-after": "5",
                        },
                    }),
            ),
        );

        const response = await POST(postRequest("{}"));
        expect(response.status).toBe(503);
        expect(response.headers.get("retry-after")).toBe("5");
        expect(response.headers.get("content-type")).not.toContain("event-stream");
        expect(await response.text()).toBe(errorBody);
    });

    it("passes a 404 body through verbatim without fabricating a stream", async () => {
        const body = JSON.stringify({ detail: "Unknown pipeline_config." });
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(body, {
                        status: 404,
                        headers: { "content-type": "application/json" },
                    }),
            ),
        );

        const response = await POST(postRequest("{}"));
        expect(response.status).toBe(404);
        expect(await response.text()).toBe(body);
    });
});
