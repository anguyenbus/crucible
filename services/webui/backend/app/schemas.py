"""Pydantic v2 request/response schemas and the document status enum.

The `DocumentStatus` enum is the COARSE lifecycle, reused by the upload bridge:
- `pending`  — accepted; ingest runs asynchronously in the background
- `indexed`  — ingest ok (`skipped=false`), chunks written
- `skipped`  — dedup skip (ingestion returned `skipped=true`)
- `failed`   — ingestion returned a typed error (see `failure_reason`/`failure_code`)

While `pending`, the FINE-GRAINED live step is carried separately by `phase`
(`DocumentPhase`) plus, during embedding, the `phase_current`/`phase_total`
sub-counters. On any terminal status the phase fields are cleared to NULL.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel

# Which document analysis to run (mirrors the orchestrator's AnalyzeMode): the
# UI's "Extract" (facts) and "Summarise" (summary) buttons drive these.
AnalyzeMode = Literal["facts", "summary", "both"]

# The live ingestion step while a document is `pending`. `queued` is the BFF's
# own pre-ingest state; the rest mirror the ingestion service's SSE phases. This
# names the KNOWN set (the frontend renders labels for these); the stored
# `DocumentRecord.phase` is deliberately typed looser (`str | None`) so that a
# NEW phase emitted by an independently-deployed ingestion never fails the BFF's
# document reads — an unknown phase is passed through, not rejected.
DocumentPhase = Literal["queued", "parsing", "chunking", "embedding", "indexing"]


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
    # Live ingestion progress (meaningful only while `status == pending`; NULL on
    # any terminal status). Typed `str` (not `DocumentPhase`) on purpose — see the
    # note on `DocumentPhase`: a phase from a newer ingestion must pass through,
    # never fail this read. `phase_current`/`phase_total` are the embedding chunk
    # counters ("chunk i of N"); NULL for the non-embedding phases.
    phase: str | None = None
    phase_current: int | None = None
    phase_total: int | None = None
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
