"""Deterministic document/chunk ID helpers and the SHA-256 dedup check.

Deterministic chunk IDs (`{doc_id}:{chunk_index}`) are the cross-cutting
foundation of this service: retries after a partial bulk failure self-heal by
overwriting the same OpenSearch `_id`s (no rollback logic anywhere),
changed-content re-ingest overwrites in place before stale chunks are pruned,
and the dedup skip can verify completeness by comparing chunk counts.

The dedup check is partial-failure-proof: an ingest is skipped ONLY when the
index already holds chunks for the same doc_id+sha256 AND their count equals
the locally computed expected chunk count (cheap tiktoken chunking of the
normalized text — no embedding calls). A partial prior run leaves fewer
chunks than expected, so it never masquerades as a completed ingest.

The dedup COUNT runs against the SAME target `index` as the write/prune
(project-scoped chat): `None` falls back to `settings.index_name` so
absent-`index` behavior is byte-identical; a per-project `index` is checked
in that index only (a fresh project index simply reports no duplicate).

POC caveat (accepted, documented — not a bug): the doc_id is derived from the
exact source URI string, so the same file ingested via a local path and via
its S3 URI yields two different doc_ids.
"""

import hashlib

from opensearchpy.exceptions import NotFoundError

from app.clients.opensearch import get_opensearch_client
from app.config import get_settings

DOC_ID_HEX_CHARS = 16


def derive_doc_id(source_uri: str) -> str:
    """Truncated SHA-256 (16 hex chars) of the canonical source URI — not a slug."""
    return hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:DOC_ID_HEX_CHARS]


def chunk_id(doc_id: str, chunk_index: int) -> str:
    """OpenSearch `_id` for a chunk document."""
    return f"{doc_id}:{chunk_index}"


def content_sha256(text: str) -> str:
    """Full SHA-256 hex digest of the normalized document content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_complete_duplicate(
    doc_id: str, sha256: str, expected_chunk_count: int, index: str | None = None, client=None
) -> bool:
    """True only when `index` already holds a COMPLETE copy of this content.

    Counts existing chunks for doc_id+sha256 and compares against the locally
    computed expected chunk count; a mismatch (partial prior run) or a missing
    index means the document must be (re-)ingested in full. `index` defaults to
    `settings.index_name` when absent (unchanged single-index behavior).
    """
    if expected_chunk_count <= 0:
        return False
    if client is None:
        client = get_opensearch_client()
    if index is None:
        index = get_settings().index_name
    query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"doc_id": doc_id}},
                    {"term": {"sha256": sha256}},
                ]
            }
        }
    }
    try:
        response = client.count(index=index, body=query)
    except NotFoundError:
        return False  # index does not exist yet — nothing indexed
    return response.get("count", 0) == expected_chunk_count
