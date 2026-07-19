import { afterEach, describe, expect, it, vi } from "vitest";
import { interpretReadyz, probeReadyz } from "./readyz";

/*
 * Group 7 — the readyz banner gate. A failing probe carries the REAL dependency
 * error text and blocks input (ready=false); a later success clears the banner
 * and re-enables input — a recoverable banner, not a hard-stop.
 */

afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

describe("readyz gate", () => {
    it("a failing probe surfaces the real dependency error and blocks input", async () => {
        const body = JSON.stringify({ status: "not_ready", detail: "OpenSearch unreachable at legal-index." });
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response(body, { status: 503, headers: { "content-type": "application/json" } })),
        );

        const state = await probeReadyz();
        expect(state.ready).toBe(false); // ⇒ chat input disabled
        expect(state.errorText).toContain("OpenSearch unreachable at legal-index.");
    });

    it("interprets a non-JSON error body as-is (no fabrication)", () => {
        const state = interpretReadyz(false, 502, "bedrock: connection refused");
        expect(state.ready).toBe(false);
        expect(state.errorText).toBe("bedrock: connection refused");
    });

    it("re-probing after recovery clears the banner and re-enables input", async () => {
        const fetchMock = vi
            .fn()
            .mockResolvedValueOnce(
                new Response(JSON.stringify({ detail: "warming up" }), {
                    status: 503,
                    headers: { "content-type": "application/json" },
                }),
            )
            .mockResolvedValueOnce(new Response(JSON.stringify({ status: "ok" }), { status: 200 }));
        vi.stubGlobal("fetch", fetchMock);

        const first = await probeReadyz();
        expect(first.ready).toBe(false);

        const second = await probeReadyz();
        expect(second.ready).toBe(true);
        expect(second.errorText).toBeNull();
        expect(fetchMock).toHaveBeenCalledTimes(2);
    });
});
