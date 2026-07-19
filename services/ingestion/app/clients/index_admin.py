"""Index lifecycle helpers: idempotent provision + delete (project-scoped chat).

Ingestion OWNS the OpenSearch index (it holds the SigV4 client and the pure
`build_index_body` mapping), so per-project index lifecycle lives here and the
BFF drives it over HTTP — the BFF never talks to OpenSearch directly.

These functions mirror `scripts/create_index.py`'s idempotent behavior
(`ensure_index` / `ensure_search_pipeline`) but take an injected client so the
endpoint and its tests share one code path with no network at test time.

Explicit provisioning is REQUIRED before first ingest: dynamic auto-create on
first write would map `content_vector` as a plain float array, NOT a
`knn_vector`, silently breaking retrieval. Provision creates the correct knn
mapping up front.
"""

from __future__ import annotations

import re
from typing import Any

from opensearchpy.exceptions import NotFoundError, RequestError

from app.clients.opensearch import build_index_body, build_search_pipeline_body

# Caller-supplied index names (the BFF's `proj-{project_id}`) must be acceptable
# OpenSearch index names: lowercase, first char a letter/digit (never a leading
# `-`/`_`/`+`), and only `[a-z0-9._-]` thereafter. This rejects uppercase and
# the OpenSearch-forbidden leading characters BEFORE any cluster call is made.
_INDEX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_INDEX_NAME_LEN = 255


class InvalidIndexNameError(ValueError):
    """The caller-supplied index name is not an acceptable OpenSearch name.

    Mapped to HTTP 400 by the API layer — a caller error, raised BEFORE any
    OpenSearch call.
    """


def validate_index_name(name: str) -> None:
    """Reject unacceptable OpenSearch index names (400 before any cluster call)."""
    if not name or len(name.encode("utf-8")) > _MAX_INDEX_NAME_LEN:
        raise InvalidIndexNameError(
            f"Invalid index name {name!r}: must be 1–{_MAX_INDEX_NAME_LEN} bytes."
        )
    if name in (".", ".."):
        raise InvalidIndexNameError(f"Invalid index name {name!r}: cannot be '.' or '..'.")
    if name[0] in "-_+":
        raise InvalidIndexNameError(
            f"Invalid index name {name!r}: cannot start with '-', '_', or '+'."
        )
    if not _INDEX_NAME_RE.fullmatch(name):
        raise InvalidIndexNameError(
            f"Invalid index name {name!r}: must be lowercase and contain only "
            "letters, digits, '.', '_', or '-'."
        )


def ensure_index(client: Any, index_name: str) -> bool:
    """Create the knn index if absent; return True if created, False if it existed.

    Idempotent (matching `scripts/create_index.py`): an existing index is a
    no-op. Falls back from the faiss HNSW engine to nmslib if the domain
    rejects faiss.
    """
    if client.indices.exists(index=index_name):
        return False
    try:
        client.indices.create(index=index_name, body=build_index_body(engine="faiss"))
    except RequestError:
        # Domain rejected the faiss HNSW method — retry once with nmslib.
        client.indices.create(index=index_name, body=build_index_body(engine="nmslib"))
    return True


def ensure_search_pipeline(client: Any, pipeline_name: str) -> None:
    """Ensure the named hybrid-search pipeline exists (idempotent)."""
    try:
        client.transport.perform_request("GET", f"/_search/pipeline/{pipeline_name}")
        return
    except NotFoundError:
        pass
    client.transport.perform_request(
        "PUT",
        f"/_search/pipeline/{pipeline_name}",
        body=build_search_pipeline_body(),
    )


def provision_index(client: Any, index_name: str, *, pipeline_name: str) -> bool:
    """Idempotently provision `index_name` with the knn mapping + search pipeline.

    Validates the name first (400 before any cluster call). Returns True when
    the index was newly created, False when it already existed (still ensures
    the pipeline either way).
    """
    validate_index_name(index_name)
    created = ensure_index(client, index_name)
    ensure_search_pipeline(client, pipeline_name)
    return created


def delete_index(client: Any, index_name: str) -> bool:
    """Drop `index_name`; a missing index is a benign no-op (idempotent delete).

    Validates the name first (400 before any cluster call). Returns True when
    an index was actually deleted, False when it did not exist.
    """
    validate_index_name(index_name)
    try:
        client.indices.delete(index=index_name)
        return True
    except NotFoundError:
        return False
