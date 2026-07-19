/**
 * readyz gate logic (Q7) — adapted from Chainlit's hard-stop into a recoverable
 * banner. Probe `GET /readyz` via the proxy; on failure the banner carries the
 * REAL dependency error text and chat input is disabled. This is NOT a
 * hard-stop: a dependency blip does not kill the session, so auto + manual
 * retry can clear the banner and re-enable input. No fake degraded mode.
 *
 * Pure and framework-free so the gate logic is unit-tested without a browser.
 */

export const READYZ_PATH = "/api/readyz";

export interface ReadyzState {
    ready: boolean;
    /** The real dependency error text to show in the banner (null when ready). */
    errorText: string | null;
}

/** Derive the gate state from a `/readyz` response's status + body. */
export function interpretReadyz(ok: boolean, status: number, body: string): ReadyzState {
    if (ok) return { ready: true, errorText: null };

    let errorText = body.trim();
    try {
        const data = JSON.parse(body) as Record<string, unknown>;
        const parts: string[] = [];
        if (typeof data.detail === "string") parts.push(data.detail);
        if (typeof data.reason === "string") parts.push(data.reason);
        if (typeof data.status === "string" && parts.length === 0) {
            parts.push(String(data.status));
        }
        if (parts.length > 0) errorText = parts.join(" — ");
    } catch {
        // Non-JSON body — keep the raw text.
    }
    if (!errorText) {
        errorText = `The pipeline is not ready (HTTP ${status}).`;
    }
    return { ready: false, errorText };
}

/** Probe the readyz proxy once and return the interpreted gate state. */
export async function probeReadyz(fetchImpl: typeof fetch = fetch): Promise<ReadyzState> {
    try {
        const response = await fetchImpl(READYZ_PATH, {
            method: "GET",
            cache: "no-store",
        });
        const body = await response.text();
        return interpretReadyz(response.ok, response.status, body);
    } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        return { ready: false, errorText: `Cannot reach the pipeline: ${detail}` };
    }
}
