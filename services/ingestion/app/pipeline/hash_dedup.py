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


# The raw-bytes gate aggregates the matching chunks by doc_id to confirm at least
# one COMPLETE indexed copy exists. A single raw_sha256 realistically maps to one
# (occasionally a few) doc_ids — the same file re-uploaded via different source
# URIs — so a generous bucket cap covers every real case without paging.
_RAW_DEDUP_MAX_DOCS = 1000


def raw_content_sha256(raw_bytes: bytes) -> str:
    """Full SHA-256 hex digest of the RAW fetched document bytes (pre-parse).

    A DISTINCT concern from `content_sha256`, which hashes the normalized text
    AFTER the expensive parse. This digest is the key of the pre-parse gate: it
    is computed on the bytes straight out of `fetch_bytes`, so an identical
    re-upload is recognised before the parser is ever called.
    """
    return hashlib.sha256(raw_bytes).hexdigest()


def find_complete_raw_duplicate(
    raw_sha256: str, index: str | None = None, client=None
) -> str | None:
    """The pre-parse gate: return an already-indexed COMPLETE copy's content sha.

    Looks up `raw_sha256` in `index` INDEPENDENT of doc_id (the same bytes via a
    different source URI still hits) and returns the normalized-text `sha256` of a
    complete indexed copy, or `None` when none exists. On a hit the caller skips
    the whole parse + embed + index cost.

    CORRECTNESS (partial-prior-run safety): the gate fires ONLY when some doc_id
    under this `raw_sha256` holds a COMPLETE chunk set — its indexed chunk count
    equals the `chunk_count` stamped identically on every one of its chunks at
    index time. A prior run that crashed mid-bulk leaves fewer chunks than
    `chunk_count`, so that bucket is NOT complete, the gate does NOT fire, and the
    document re-ingests (self-healing via deterministic `_id`s). This mirrors
    `is_complete_duplicate`'s count-equals-expected guarantee, but sources the
    expected count from the chunks themselves rather than a local re-parse — so a
    half-indexed document can never be permanently masked by the fast-path gate.
    `index` defaults to `settings.index_name` when absent (unchanged single-index
    behavior); a missing index means nothing is indexed yet.
    """
    if client is None:
        client = get_opensearch_client()
    if index is None:
        index = get_settings().index_name
    body = {
        "size": 0,
        "query": {"term": {"raw_sha256": raw_sha256}},
        "aggs": {
            "by_doc": {
                "terms": {"field": "doc_id", "size": _RAW_DEDUP_MAX_DOCS},
                "aggs": {
                    # `chunk_count` and `sha256` are identical across a doc's chunks,
                    # so `max`/top-`terms` read them off the bucket for free.
                    "expected": {"max": {"field": "chunk_count"}},
                    "content_sha": {"terms": {"field": "sha256", "size": 1}},
                },
            }
        },
    }
    try:
        response = client.search(index=index, body=body)
    except NotFoundError:
        return None  # index does not exist yet — nothing indexed
    buckets = (
        response.get("aggregations", {}).get("by_doc", {}).get("buckets", [])
    )
    for bucket in buckets:
        expected = bucket.get("expected", {}).get("value")
        if expected is None:
            continue  # pre-gate chunks without chunk_count — cannot confirm complete
        if bucket.get("doc_count", 0) != int(expected):
            continue  # partial prior run for this doc_id — not a complete copy
        sha_buckets = bucket.get("content_sha", {}).get("buckets", [])
        if sha_buckets:
            return sha_buckets[0]["key"]
    return None
