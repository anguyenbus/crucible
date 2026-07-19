"""Documents read/delete endpoints + the synchronous upload -> storage -> ingest
bridge.

Documents are always project-scoped (no standalone-document surface). Nesting
stays at <=3 levels. The upload endpoint is honest and SYNCHRONOUS: it validates
the file type, enforces a size bound, stores the bytes (S3 or local fallback),
calls ingestion and BLOCKS on it, then persists the result. Ingestion's typed
errors map to a PERSISTED `failed` document status (with
`failure_reason`/`failure_code`) and are RETURNED as the document record --
never swallowed as an opaque 500.

Accepted formats (project-scoped chat): markdown AND PDF. The upload routes to
ingestion with the project's `index` target (`proj-{id}`); ingestion parses PDF
natively (pypdf) and markdown as before. DOCX/image/OCR remain out of scope.

The document-detail view adds two READ endpoints:
  - `/{document_id}/file` streams the stored bytes back inline (embeddable in an
    `<object>`/`<iframe>`), so the left pane can render the original PDF/markdown.
  - `/{document_id}/text` returns the ACTUAL indexed chunk text from ingestion
    (the index owner) for the right pane — honest empty when nothing was indexed.
"""

from pathlib import PurePosixPath
from sqlite3 import Connection

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)

from app import analyze_client, index_client, ingest_client, storage, store
from app.config import get_settings
from app.db import connect
from app.deps import get_conn
from app.schemas import (
    AnalyzeMode,
    DocumentFacts,
    DocumentRecord,
    DocumentStatus,
    ProjectDetail,
)

router = APIRouter(prefix="/projects", tags=["documents"])

_SUPPORTED_EXTENSIONS = (".md", ".markdown", ".pdf")
_SUPPORTED_CONTENT_TYPES = (
    "text/markdown",
    "text/x-markdown",
    "application/pdf",
)

# Coarse pre-filter, consistent with ingestion's limits: ingestion caps a
# document at max_chunks_per_doc=100 (~800 tokens/chunk) and Titan bounds each
# chunk at ~50k chars. 100 chunks x ~800 tokens x ~5 chars/token ~= 400 KB of
# indexable content; we allow up to 1 MiB of headroom before storing and let
# ingestion make the precise chunk-count call (its 400 maps to status=failed).
MAX_UPLOAD_BYTES = 1_048_576


def _is_supported(upload: UploadFile) -> bool:
    name = (upload.filename or "").lower()
    if name.endswith(_SUPPORTED_EXTENSIONS):
        return True
    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    return content_type in _SUPPORTED_CONTENT_TYPES


def _require_project(conn: Connection, project_id: str) -> ProjectDetail:
    project = store.get_project(conn, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return project


def _require_document(
    conn: Connection, project_id: str, document_id: str
) -> DocumentRecord:
    document = store.get_document(conn, project_id, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="document not found"
        )
    return document


@router.get("/{project_id}/documents", response_model=list[DocumentRecord])
def list_documents(
    project_id: str, conn: Connection = Depends(get_conn)
) -> list[DocumentRecord]:
    _require_project(conn, project_id)
    return store.list_documents(conn, project_id)


@router.get(
    "/{project_id}/documents/{document_id}", response_model=DocumentRecord
)
def get_document(
    project_id: str, document_id: str, conn: Connection = Depends(get_conn)
) -> DocumentRecord:
    return _require_document(conn, project_id, document_id)


@router.get("/{project_id}/documents/{document_id}/file")
def get_document_file(
    project_id: str, document_id: str, conn: Connection = Depends(get_conn)
) -> Response:
    """Stream the stored bytes back INLINE so the left pane can embed them.

    Content-Type is derived from the filename extension; the response is
    `Content-Disposition: inline` and deliberately sets NO `X-Frame-Options`, so
    a browser may render it inside an `<object>`/`<iframe>`. Missing stored bytes
    are an honest 404 (typed), never an opaque 500.
    """
    document = _require_document(conn, project_id, document_id)
    try:
        data, content_type = storage.read_stored(document.storage_uri)
    except storage.StoredObjectMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="stored file is no longer available",
        ) from exc
    # Quote the filename so names with spaces/commas stay a single header value.
    disposition = f'inline; filename="{document.filename}"'
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/{project_id}/documents/{document_id}/text")
def get_document_text(
    project_id: str, document_id: str, conn: Connection = Depends(get_conn)
) -> dict:
    """Return the ACTUAL indexed chunk text for the right pane (via ingestion).

    A document without an `ingest_doc_id` (e.g. a failed upload) never made it
    into the index, so we return an honest empty result WITHOUT calling
    ingestion. Otherwise we ask ingestion (the index owner) over HTTP; its typed
    failures surface with their real status, not a swallowed 500.
    """
    document = _require_document(conn, project_id, document_id)
    if document.ingest_doc_id is None:
        return {"chunk_count": 0, "text": "", "chunks": []}

    project = _require_project(conn, project_id)
    try:
        return index_client.get_document_chunks(
            project.index_name, document.ingest_doc_id, get_settings().ingestion_url
        )
    except index_client.IndexServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post(
    "/{project_id}/documents/{document_id}/analyze", response_model=DocumentFacts
)
def analyze_document(
    project_id: str,
    document_id: str,
    mode: AnalyzeMode = "both",
    conn: Connection = Depends(get_conn),
) -> DocumentFacts:
    """Extract facts and/or summarise one document (on demand).

    ``mode`` (`facts` / `summary` / `both`) drives the UI's independent
    "Extract" and "Summarise" buttons. Non-RAG: we fetch the document's ACTUAL
    indexed text from ingestion (the index owner) and forward it to the
    orchestrator's `/analyze` — the BFF never runs an LLM itself. A document
    that never indexed (no `ingest_doc_id` / no chunks) is an honest 422, not an
    empty analysis. The orchestrator's typed failures surface with their real
    status, never a swallowed 500.
    """
    document = _require_document(conn, project_id, document_id)
    project = _require_project(conn, project_id)
    settings = get_settings()

    if document.ingest_doc_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="this document has no indexed text to analyse",
        )

    try:
        chunks = index_client.get_document_chunks(
            project.index_name, document.ingest_doc_id, settings.ingestion_url
        )
    except index_client.IndexServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    text = (chunks.get("text") or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="this document has no indexed text to analyse",
        )

    try:
        result = analyze_client.analyze_text(
            text, settings.orchestrator_url, mode=mode
        )
    except analyze_client.AnalyzeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return DocumentFacts(
        summary=result.summary,
        facts=result.facts,
        model_id=result.model_id,
        truncated=result.truncated,
    )


@router.delete(
    "/{project_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_document(
    project_id: str, document_id: str, conn: Connection = Depends(get_conn)
) -> None:
    """Delete a document: its indexed chunks, its stored file, then its row.

    Order matters. The row is dropped ONLY AFTER the index cleanup succeeds, so
    a "deleted" document can never linger as retrievable/citable chunks: if the
    chunk delete fails, we surface a 502 and keep the row (the user can retry).
    The stored-file delete is best-effort (never blocks) — the authoritative
    cleanup is the index + row. A document that never indexed (`ingest_doc_id`
    is NULL) skips the chunk step.
    """
    document = _require_document(conn, project_id, document_id)
    project = _require_project(conn, project_id)
    settings = get_settings()

    if document.ingest_doc_id:
        try:
            index_client.delete_document_chunks(
                project.index_name, document.ingest_doc_id, settings.ingestion_url
            )
        except index_client.IndexServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"could not remove the document's chunks from the index: {exc.message}",
            ) from exc

    # Best-effort: don't orphan the uploaded bytes, but never block the delete.
    storage.delete_stored(document.storage_uri)

    if not store.delete_document(conn, project_id, document_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="document not found"
        )


def _run_ingest_job(
    *,
    document_id: str,
    storage_uri: str,
    index_name: str,
    ingestion_url: str,
    db_path: str,
) -> None:
    """Background worker: stream ingestion, recording each phase, then the result.

    Runs AFTER the upload response is sent (FastAPI `BackgroundTasks`), so it
    opens its OWN short-lived connection — the request-scoped one is already
    closed. Every event moves the document's live `phase`; the terminal `done`
    persists `indexed`/`skipped` and a typed `IngestionError` persists `failed`
    (never an unhandled crash — the row must always reach a terminal status).
    """
    conn = connect(db_path)
    try:

        def on_phase(event: dict) -> None:
            store.update_document_phase(
                conn,
                document_id,
                phase=event["phase"],
                phase_current=event.get("current"),
                phase_total=event.get("total"),
            )

        try:
            result = ingest_client.ingest_stream(
                storage_uri, ingestion_url, index=index_name, on_phase=on_phase
            )
        except ingest_client.IngestionError as exc:
            store.update_document_result(
                conn,
                document_id,
                status=DocumentStatus.failed,
                failure_reason=exc.message,
                failure_code=exc.status_code,
            )
            return
        except Exception as exc:  # noqa: BLE001 — never leave a doc stuck pending
            store.update_document_result(
                conn,
                document_id,
                status=DocumentStatus.failed,
                failure_reason=f"unexpected ingestion error: {exc}",
                failure_code=500,
            )
            return

        store.update_document_result(
            conn,
            document_id,
            status=DocumentStatus.skipped if result.skipped else DocumentStatus.indexed,
            ingest_doc_id=result.doc_id,
            sha256=result.sha256,
            chunks_indexed=result.chunks_indexed,
            skipped=result.skipped,
        )
    finally:
        conn.close()


@router.post(
    "/{project_id}/documents",
    response_model=DocumentRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_document(
    project_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    conn: Connection = Depends(get_conn),
) -> DocumentRecord:
    """Accept ONE upload and ingest it ASYNCHRONOUSLY (returns 202 immediately).

    Cheap, deterministic rejections stay synchronous (unsupported type / over the
    size limit → 400). Otherwise the bytes are stored, a `pending`/`queued`
    document row is created, and the ingest runs in a background job that streams
    per-phase progress into the row. The client uploads multiple files by firing
    several of these requests concurrently and POLLS `GET .../documents` until
    each row reaches a terminal status.
    """
    project = _require_project(conn, project_id)

    if not _is_supported(file):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only markdown (.md/.markdown) and PDF (.pdf) uploads are accepted",
        )

    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"file is {len(data)} bytes, over the {MAX_UPLOAD_BYTES}-byte limit"
            ),
        )

    filename = PurePosixPath(file.filename or "upload.md").name
    storage_uri = storage.store_upload(project_id, filename, data)

    document = store.create_document(
        conn,
        project_id=project_id,
        filename=filename,
        size_bytes=len(data),
        storage_uri=storage_uri,
        status=DocumentStatus.pending,
        phase="queued",
    )

    background_tasks.add_task(
        _run_ingest_job,
        document_id=document.id,
        storage_uri=storage_uri,
        index_name=project.index_name,
        ingestion_url=get_settings().ingestion_url,
        db_path=get_settings().db_path,
    )

    return document
