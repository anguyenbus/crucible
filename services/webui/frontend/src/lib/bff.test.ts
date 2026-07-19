import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    bffBaseUrl,
    compareDocuments,
    createProject,
    deleteProject,
    documentFileUrl,
    getDocumentText,
    listProjects,
    renameProject,
    uploadDocument,
    type BffDocument,
} from "./bff";
import { SYNCHRONOUS_INGEST_LABEL, describeStatus } from "./documentStatus";

/*
 * Group 5 — the typed BFF client + honest per-document status mapping, tested
 * browser-free (mirrors the Phase-1 readyz/streamClient tests). The client
 * targets NEXT_PUBLIC_BFF_URL; the status mapping renders indexed/skipped/failed
 * honestly and the upload path blocks on synchronous ingest (no fake progress).
 */

function okJson(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
    });
}

function baseDocument(overrides: Partial<BffDocument> = {}): BffDocument {
    return {
        id: "d1",
        project_id: "p1",
        filename: "a.md",
        size_bytes: 10,
        storage_uri: "/tmp/a.md",
        status: "indexed",
        ingest_doc_id: "doc123",
        sha256: "abc",
        chunks_indexed: 4,
        skipped: false,
        failure_reason: null,
        failure_code: null,
        created_at: "t",
        updated_at: "t",
        ...overrides,
    };
}

const OLD_ENV = process.env.NEXT_PUBLIC_BFF_URL;

beforeEach(() => {
    process.env.NEXT_PUBLIC_BFF_URL = "http://bff.test:8002";
});

afterEach(() => {
    process.env.NEXT_PUBLIC_BFF_URL = OLD_ENV;
    vi.restoreAllMocks();
});

describe("bff client requests", () => {
    it("builds create/list/rename/delete requests against NEXT_PUBLIC_BFF_URL", async () => {
        const calls: Array<{ url: string; init: RequestInit }> = [];
        const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
            calls.push({ url, init });
            if (init.method === "DELETE") return new Response(null, { status: 204 });
            return okJson({ id: "p1", name: "N", document_count: 0, created_at: "t", updated_at: "t", documents: [] });
        }) as unknown as typeof fetch;

        await createProject("Matter A", fetchImpl);
        await listProjects(fetchImpl);
        await renameProject("p1", "Matter A2", fetchImpl);
        await deleteProject("p1", fetchImpl);

        expect(calls[0]).toMatchObject({ url: "http://bff.test:8002/projects", init: { method: "POST" } });
        expect(JSON.parse(calls[0].init.body as string)).toEqual({ name: "Matter A" });
        expect(calls[1]).toMatchObject({ url: "http://bff.test:8002/projects", init: { method: "GET" } });
        expect(calls[2]).toMatchObject({ url: "http://bff.test:8002/projects/p1", init: { method: "PATCH" } });
        expect(calls[3]).toMatchObject({ url: "http://bff.test:8002/projects/p1", init: { method: "DELETE" } });
    });

    it("uploads markdown as multipart and returns the accepted queued doc (202)", async () => {
        let captured: { url: string; init: RequestInit } | null = null;
        const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
            captured = { url, init };
            // Async ingest: the BFF accepts the upload and returns a queued doc.
            return okJson(baseDocument({ status: "pending", phase: "queued" }), 202);
        }) as unknown as typeof fetch;

        const file = new File(["# Hello"], "note.md", { type: "text/markdown" });
        const doc = await uploadDocument("p1", file, fetchImpl);

        expect(captured!.url).toBe("http://bff.test:8002/projects/p1/documents");
        expect(captured!.init.method).toBe("POST");
        expect(captured!.init.body).toBeInstanceOf(FormData);
        expect((captured!.init.body as FormData).get("file")).toBeInstanceOf(File);
        expect(doc.status).toBe("pending");
        expect(doc.phase).toBe("queued");
    });

    it("throws a typed BffError carrying the real detail on a non-2xx", async () => {
        const fetchImpl = vi.fn(async () =>
            okJson({ detail: "name is required" }, 400),
        ) as unknown as typeof fetch;

        await expect(createProject("  ", fetchImpl)).rejects.toMatchObject({
            detail: "name is required",
            status: 400,
        });
    });

    it("defaults the base URL when NEXT_PUBLIC_BFF_URL is unset", () => {
        delete process.env.NEXT_PUBLIC_BFF_URL;
        expect(bffBaseUrl()).toBe("http://localhost:8002");
    });
});

describe("document detail client", () => {
    it("builds the file URL against NEXT_PUBLIC_BFF_URL with encoding", () => {
        expect(documentFileUrl("p1", "d1")).toBe(
            "http://bff.test:8002/projects/p1/documents/d1/file",
        );
        // Path segments are URL-encoded (no raw slashes/spaces leak through).
        expect(documentFileUrl("p 1", "d/1")).toBe(
            "http://bff.test:8002/projects/p%201/documents/d%2F1/file",
        );
    });

    it("fetches and parses the extracted-text response", async () => {
        let captured: { url: string; init: RequestInit } | null = null;
        const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
            captured = { url, init };
            return okJson({
                chunk_count: 2,
                text: "one\n\ntwo",
                chunks: [
                    { chunk_index: 0, content: "one" },
                    { chunk_index: 1, content: "two" },
                ],
            });
        }) as unknown as typeof fetch;

        const result = await getDocumentText("p1", "d1", fetchImpl);

        expect(captured!.url).toBe("http://bff.test:8002/projects/p1/documents/d1/text");
        expect(captured!.init.method).toBe("GET");
        expect(result.chunk_count).toBe(2);
        expect(result.text).toBe("one\n\ntwo");
        expect(result.chunks).toHaveLength(2);
    });

    it("parses an honest empty extracted-text response", async () => {
        const fetchImpl = vi.fn(async () =>
            okJson({ chunk_count: 0, text: "", chunks: [] }),
        ) as unknown as typeof fetch;

        const result = await getDocumentText("p1", "d1", fetchImpl);
        expect(result.chunk_count).toBe(0);
        expect(result.text).toBe("");
        expect(result.chunks).toEqual([]);
    });

    it("surfaces a typed BffError when the text endpoint fails", async () => {
        const fetchImpl = vi.fn(async () =>
            okJson({ detail: "ingestion unreachable" }, 502),
        ) as unknown as typeof fetch;

        await expect(getDocumentText("p1", "d1", fetchImpl)).rejects.toMatchObject({
            detail: "ingestion unreachable",
            status: 502,
        });
    });
});

describe("compare documents client", () => {
    it("POSTs the two document ids as JSON and parses the contradiction report", async () => {
        let captured: { url: string; init: RequestInit } | null = null;
        const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
            captured = { url, init };
            return okJson({
                document_a: "a.md",
                document_b: "b.md",
                contradictions: [
                    {
                        type: "temporal",
                        description: "Different dates.",
                        quote_a: "Jan 15",
                        quote_b: "end of Q1",
                    },
                ],
                model_id: "au.anthropic.claude-sonnet-4-6",
                truncated: false,
            });
        }) as unknown as typeof fetch;

        const result = await compareDocuments("p1", "d1", "d2", fetchImpl);

        expect(captured!.url).toBe("http://bff.test:8002/projects/p1/compare");
        expect(captured!.init.method).toBe("POST");
        expect(JSON.parse(captured!.init.body as string)).toEqual({
            document_id_a: "d1",
            document_id_b: "d2",
        });
        expect(result.document_a).toBe("a.md");
        expect(result.contradictions).toHaveLength(1);
        expect(result.contradictions[0].type).toBe("temporal");
        expect(result.contradictions[0].quote_b).toBe("end of Q1");
    });

    it("parses an honest empty (no contradictions) report", async () => {
        const fetchImpl = vi.fn(async () =>
            okJson({
                document_a: "a.md",
                document_b: "b.md",
                contradictions: [],
                model_id: "m",
                truncated: false,
            }),
        ) as unknown as typeof fetch;

        const result = await compareDocuments("p1", "d1", "d2", fetchImpl);
        expect(result.contradictions).toEqual([]);
    });

    it("surfaces a typed BffError (400) when comparing a document with itself", async () => {
        const fetchImpl = vi.fn(async () =>
            okJson({ detail: "pick two different documents to compare" }, 400),
        ) as unknown as typeof fetch;

        await expect(compareDocuments("p1", "d1", "d1", fetchImpl)).rejects.toMatchObject({
            detail: "pick two different documents to compare",
            status: 400,
        });
    });
});

describe("honest per-document status mapping", () => {
    it("maps indexed with its chunk count", () => {
        const view = describeStatus(baseDocument({ status: "indexed", chunks_indexed: 4 }));
        expect(view.tone).toBe("indexed");
        expect(view.detail).toBe("4 chunks indexed");
        expect(view.blocking).toBe(false);
    });

    it("maps a dedup skip honestly", () => {
        const view = describeStatus(baseDocument({ status: "skipped", skipped: true, chunks_indexed: 0 }));
        expect(view.tone).toBe("skipped");
        expect(view.detail).toContain("dedup");
    });

    it("maps a failure to its real failure_reason", () => {
        const view = describeStatus(
            baseDocument({ status: "failed", failure_code: 502, failure_reason: "Bedrock upstream failure" }),
        );
        expect(view.tone).toBe("failed");
        expect(view.detail).toBe("Bedrock upstream failure");
    });

    it("falls back to a generic blocking state for a pending doc with no phase yet", () => {
        const view = describeStatus(baseDocument({ status: "pending", phase: null }));
        expect(view.blocking).toBe(true);
        expect(view.detail).toBe(SYNCHRONOUS_INGEST_LABEL);
        // No fabricated percentage / progress number anywhere in the copy.
        expect(view.detail).not.toMatch(/\d+\s*%/);
    });

    it("renders the live phase for a pending doc (label + detail, still blocking)", () => {
        const view = describeStatus(baseDocument({ status: "pending", phase: "parsing" }));
        expect(view.tone).toBe("pending");
        expect(view.blocking).toBe(true);
        expect(view.label).toBe("Parsing…");
        expect(view.detail).toBe("Parsing…");
    });

    it("shows real embedding sub-progress (chunk i of N — the service's own count)", () => {
        const view = describeStatus(
            baseDocument({
                status: "pending",
                phase: "embedding",
                phase_current: 3,
                phase_total: 8,
            }),
        );
        expect(view.label).toBe("Embedding…");
        expect(view.detail).toBe("Embedding chunk 3 of 8…");
        // It is a real i/N count, not a fabricated percentage bar.
        expect(view.detail).not.toMatch(/\d+\s*%/);
    });

    it("degrades gracefully if embedding counters are missing", () => {
        const view = describeStatus(baseDocument({ status: "pending", phase: "embedding" }));
        expect(view.detail).toBe("Embedding…");
    });

    it("humanizes an unknown phase from a newer ingestion (no 'undefined')", () => {
        // Forward-compat: a phase this UI doesn't know is titled, not broken.
        const view = describeStatus(
            baseDocument({ status: "pending", phase: "reranking" as never }),
        );
        expect(view.blocking).toBe(true);
        expect(view.label).toBe("Reranking…");
        expect(view.detail).toBe("Reranking…");
        expect(view.detail).not.toMatch(/undefined/);
    });
});
