"""Pydantic v2 request/response schemas and the document status enum.

The `DocumentStatus` enum is defined here for reuse by the upload bridge:
- `pending`  — transient, before the synchronous ingest call resolves
- `indexed`  — ingest ok (`skipped=false`), chunks written
- `skipped`  — dedup skip (ingestion returned `skipped=true`)
- `failed`   — ingestion returned a typed error (see `failure_reason`/`failure_code`)
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel

# Which document analysis to run (mirrors the orchestrator's AnalyzeMode): the
# UI's "Extract" (facts) and "Summarise" (summary) buttons drive these.
AnalyzeMode = Literal["facts", "summary", "both"]


class DocumentStatus(str, Enum):
    pending = "pending"
    indexed = "indexed"
    skipped = "skipped"
    failed = "failed"


class ProjectCreate(BaseModel):
    name: str


class ProjectUpdate(BaseModel):
    name: str


class DocumentRecord(BaseModel):
    id: str
    project_id: str
    filename: str
    size_bytes: int
    storage_uri: str
    status: DocumentStatus
    ingest_doc_id: str | None = None
    sha256: str | None = None
    chunks_indexed: int | None = None
    skipped: bool | None = None
    failure_reason: str | None = None
    failure_code: int | None = None
    created_at: str
    updated_at: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    # Per-project OpenSearch index (`proj-{id}`); the frontend scopes chat to it.
    index_name: str
    document_count: int
    created_at: str
    updated_at: str


class ProjectDetail(ProjectSummary):
    documents: list[DocumentRecord]


class DocumentFacts(BaseModel):
    """LLM analysis of one document: a summary + extracted discrete facts.

    Produced on demand by the orchestrator's non-RAG `/analyze` over the
    document's indexed text; `truncated` flags that the text exceeded the
    analysis cap and was shortened before the model saw it.
    """

    summary: str
    facts: list[str]
    model_id: str
    truncated: bool
