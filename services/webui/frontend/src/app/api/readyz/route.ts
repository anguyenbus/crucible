/**
 * GET proxy for the orchestrator's `GET /readyz`. Passes the status and the
 * REAL dependency error body through verbatim; the app shell (Group 7) renders
 * it as a banner (not a hard-stop). No fabrication, no degraded fake mode.
 */

import { orchestratorUrl } from "@/lib/serverEnv";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
    let upstream: Response;
    try {
        upstream = await fetch(`${orchestratorUrl()}/readyz`, {
            method: "GET",
            headers: { Accept: "application/json" },
            signal: request.signal,
            cache: "no-store",
        });
    } catch (error) {
        // The orchestrator is unreachable (connection refused / DNS / abort).
        // Surface an honest 503 with the real reason so the banner can show it.
        const detail = error instanceof Error ? error.message : String(error);
        return new Response(
            JSON.stringify({ status: "unreachable", detail }),
            {
                status: 503,
                headers: {
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                },
            },
        );
    }

    const body = await upstream.text();
    const headers = new Headers();
    const contentType = upstream.headers.get("content-type");
    if (contentType) headers.set("Content-Type", contentType);
    const retryAfter = upstream.headers.get("retry-after");
    if (retryAfter) headers.set("Retry-After", retryAfter);
    headers.set("Cache-Control", "no-store");

    return new Response(body, { status: upstream.status, headers });
}
