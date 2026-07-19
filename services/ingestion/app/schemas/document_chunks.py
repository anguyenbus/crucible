"""Response models for a single document's indexed chunks (detail view).

The BFF's document-detail view asks ingestion (the index owner) for the ACTUAL
chunk text it stored for one `doc_id`, so the right-hand pane shows what was
indexed — never a re-parse or fabrication. `text` is the ordered chunk `content`
joined by a blank line; `chunk_count` is honest (0 when nothing was indexed).
"""

from pydantic import BaseModel


class DocumentChunk(BaseModel):
    """One indexed chunk and its genuinely per-chunk metadata: the OpenSearch
    `_id` (citation key, e.g. `{doc_id}:{chunk_index}`), ordinal position,
    stored text `content`, and the `created_at` indexing timestamp. Document-level
    provenance (doc_id, source_uri, sha256) is identical across a doc's chunks and
    lives on the response, not repeated here."""

    id: str
    chunk_index: int
    content: str
    created_at: str | None = None


class DocumentChunksResponse(BaseModel):
    """A document's chunks in `chunk_index` order plus the joined `text`.

    A missing index or a doc with no chunks yields `chunk_count == 0`, an empty
    `chunks` list, and empty `text` — a 200, not an error.
    """

    index: str
    doc_id: str
    chunk_count: int
    text: str
    chunks: list[DocumentChunk]
    # Dimension of each chunk's `content_vector` embedding (constant for the
    # index; Titan v2 = 1024). Every chunk carries a vector of this size.
    embedding_dims: int


class DeleteDocumentChunksResponse(BaseModel):
    """Result of deleting one document's chunks from an index (delete-by-query).

    Idempotent: a missing index or an already-absent document yields
    `deleted == 0` and a 200, not an error.
    """

    index: str
    doc_id: str
    deleted: int
