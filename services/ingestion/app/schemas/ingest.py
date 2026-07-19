"""Request/response models for POST /ingest."""

from pydantic import BaseModel


class IngestRequest(BaseModel):
    """`source` is an s3://bucket/key.md URI (canonical) or a local absolute path.

    `index` is the OPTIONAL target OpenSearch index (project-scoped chat): the
    BFF composes and owns the deterministic per-project index name
    (`proj-{project_id}`), so a single explicit `index` is sufficient — no
    `project_id` field is added (it would be redundant provenance). ABSENT
    means the service-default single index (`settings.index_name`, default
    `genai-ingestion-md`), byte-identical to today.
    """

    source: str
    doc_id: str | None = None
    index: str | None = None


class IngestResponse(BaseModel):
    doc_id: str
    sha256: str
    chunks_indexed: int
    skipped: bool
