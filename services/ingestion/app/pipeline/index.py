"""Bulk indexing stage: write embedded chunks into the OpenSearch index.

Every chunk is indexed under the deterministic `_id = "{doc_id}:{chunk_index}"`,
so a retry after a partial bulk failure simply overwrites the same documents
— self-healing by design, no rollback logic anywhere. `refresh=wait_for`
makes a just-ingested document immediately searchable.

Per-item `_bulk` failures are never silently dropped: they are collected and
raised as `BulkIndexError` with details for the API layer's 502 body.

The target `index` is an OPTIONAL per-request argument (project-scoped chat):
`None` falls back to `settings.index_name` (default `genai-ingestion-md`), so
absent-`index` write + prune behavior is byte-identical to today; a supplied
name (the BFF's `proj-{project_id}`) writes to and prunes within THAT index
only.
"""

from datetime import datetime, timezone

from app.clients.opensearch import get_opensearch_client
from app.config import get_settings
from app.pipeline.hash_dedup import chunk_id


class BulkIndexError(RuntimeError):
    """One or more chunks failed to index; `failures` holds per-item details."""

    def __init__(self, message: str, failures: list[dict]):
        super().__init__(message)
        self.failures = failures


def build_bulk_actions(
    chunks: list[str],
    vectors: list[list[float]],
    *,
    doc_id: str,
    source_uri: str,
    sha256: str,
    index_name: str | None = None,
    created_at: str | None = None,
) -> list[dict]:
    """Action/document pairs for the `_bulk` API, with deterministic `_id`s."""
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks ({len(chunks)}) and vectors ({len(vectors)}) must align"
        )
    if index_name is None:
        index_name = get_settings().index_name
    if created_at is None:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    actions: list[dict] = []
    for i, (content, vector) in enumerate(zip(chunks, vectors)):
        actions.append({"index": {"_index": index_name, "_id": chunk_id(doc_id, i)}})
        actions.append(
            {
                "content": content,
                "content_vector": vector,
                "doc_id": doc_id,
                "source_uri": source_uri,
                "sha256": sha256,
                "chunk_index": i,
                "created_at": created_at,
            }
        )
    return actions


def index_chunks(
    chunks: list[str],
    vectors: list[list[float]],
    *,
    doc_id: str,
    source_uri: str,
    sha256: str,
    index: str | None = None,
    client=None,
) -> int:
    """Bulk-index all chunks into `index`; return the number indexed.

    `index` defaults to `settings.index_name` when absent (unchanged single-
    index behavior). Raises `BulkIndexError` with per-item details if any item
    fails.
    """
    if not chunks:
        return 0
    if client is None:
        client = get_opensearch_client()
    if index is None:
        index = get_settings().index_name

    actions = build_bulk_actions(
        chunks,
        vectors,
        doc_id=doc_id,
        source_uri=source_uri,
        sha256=sha256,
        index_name=index,
    )
    response = client.bulk(body=actions, refresh="wait_for")

    if response.get("errors"):
        failures = [
            {
                "_id": result.get("_id"),
                "status": result.get("status"),
                "error": result.get("error"),
            }
            for item in response.get("items", [])
            if (result := item.get("index", {})).get("error")
        ]
        raise BulkIndexError(
            f"{len(failures)} of {len(chunks)} chunks failed to index "
            f"(doc_id {doc_id}).",
            failures,
        )
    return len(chunks)


def prune_stale_chunks(
    doc_id: str, *, keep_sha256: str, index: str | None = None, client=None
) -> int:
    """Delete chunks of `doc_id` in `index` whose sha256 differs from the new one.

    Called ONLY after bulk indexing of the new version has succeeded
    (index-then-prune): new chunks overwrite the same deterministic `_id`s in
    place, and this pass removes leftovers — e.g. trailing chunk indexes when
    the new version is shorter. Deleting first is never acceptable; the worst
    case must be stale-but-searchable content, not a data-loss window.

    `index` defaults to `settings.index_name` when absent (unchanged single-
    index behavior). Returns the number of stale chunks deleted.
    """
    if client is None:
        client = get_opensearch_client()
    if index is None:
        index = get_settings().index_name
    response = client.delete_by_query(
        index=index,
        body={
            "query": {
                "bool": {
                    "filter": [{"term": {"doc_id": doc_id}}],
                    "must_not": [{"term": {"sha256": keep_sha256}}],
                }
            }
        },
        conflicts="proceed",
        refresh=True,
    )
    return response.get("deleted", 0)
