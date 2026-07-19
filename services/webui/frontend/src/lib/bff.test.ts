import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    bffBaseUrl,
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

    it("uploads markdown as multipart to the project documents endpoint", async () => {
        let captured: { url: string; init: RequestInit } | null = null;
        const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
            captured = { url, init };
            return okJson(baseDocument({ status: "indexed" }), 201);
        }) as unknown as typeof fetch;

        const file = new File(["# Hello"], "note.md", { type: "text/markdown" });
        const doc = await uploadDocument("p1", file, fetchImpl);

        expect(captured!.url).toBe("http://bff.test:8002/projects/p1/documents");
        expect(captured!.init.method).toBe("POST");
        expect(captured!.init.body).toBeInstanceOf(FormData);
        expect((captured!.init.body as FormData).get("file")).toBeInstanceOf(File);
        expect(doc.status).toBe("indexed");
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

    it("shows an honest blocking state while ingest is pending (no fake progress)", () => {
        const view = describeStatus(baseDocument({ status: "pending" }));
        expect(view.blocking).toBe(true);
        expect(view.detail).toBe(SYNCHRONOUS_INGEST_LABEL);
        // No fabricated percentage / progress number anywhere in the copy.
        expect(view.detail).not.toMatch(/\d+\s*%/);
    });
});
