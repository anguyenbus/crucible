"""Request/response models for POST /ingest."""

from pydantic import BaseModel


class IngestRequest(BaseModel):
    """`source` is an s3://bucket/key.md URI (canonical) or a local absolute path."""

    source: str
    doc_id: str | None = None


class IngestResponse(BaseModel):
    doc_id: str
    sha256: str
    chunks_indexed: int
    skipped: bool
