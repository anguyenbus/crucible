"""Thin data-access layer over the SQLite store (no ORM).

Every function takes an open `sqlite3.Connection` (opened per request with
`PRAGMA foreign_keys=ON`, see `app.db`). All writes use parameterized queries;
user input is never interpolated into SQL. Row dicts are shaped to match the
Pydantic schemas in `app.schemas`.

Each project gets a deterministic per-project OpenSearch index name,
`proj-{id}` (the lowercase hex UUID PK is already a valid OpenSearch index
name). The name is STORED (not merely computed) so a future scheme change is a
migration, not a silent break.
"""

import sqlite3
import uuid
from datetime import datetime, timezone

from app.schemas import DocumentRecord, DocumentStatus, ProjectDetail, ProjectSummary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def index_name_for(project_id: str) -> str:
    """The deterministic per-project OpenSearch index name (`proj-{id}`)."""
    return f"proj-{project_id}"


# --- Projects ---------------------------------------------------------------


def create_project(conn: sqlite3.Connection, name: str) -> ProjectSummary:
    now = _now()
    project_id = _new_id()
    index_name = index_name_for(project_id)
    conn.execute(
        "INSERT INTO projects (id, name, index_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, name, index_name, now, now),
    )
    conn.commit()
    return ProjectSummary(
        id=project_id,
        name=name,
        index_name=index_name,
        document_count=0,
        created_at=now,
        updated_at=now,
    )


def list_projects(conn: sqlite3.Connection) -> list[ProjectSummary]:
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.index_name, p.created_at, p.updated_at,
               COUNT(d.id) AS document_count
        FROM projects p
        LEFT JOIN documents d ON d.project_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
        """
    ).fetchall()
    return [
        ProjectSummary(
            id=row["id"],
            name=row["name"],
            index_name=row["index_name"],
            document_count=row["document_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


def get_project(conn: sqlite3.Connection, project_id: str) -> ProjectDetail | None:
    row = conn.execute(
        "SELECT id, name, index_name, created_at, updated_at FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    documents = list_documents(conn, project_id)
    return ProjectDetail(
        id=row["id"],
        name=row["name"],
        index_name=row["index_name"],
        document_count=len(documents),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        documents=documents,
    )


def rename_project(
    conn: sqlite3.Connection, project_id: str, name: str
) -> ProjectDetail | None:
    row = conn.execute(
        "SELECT id FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
        (name, _now(), project_id),
    )
    conn.commit()
    return get_project(conn, project_id)


def delete_project(conn: sqlite3.Connection, project_id: str) -> bool:
    """Delete a project; its document rows cascade (ON DELETE CASCADE)."""
    cursor = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    return cursor.rowcount > 0


# --- Documents --------------------------------------------------------------


def _row_to_document(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        id=row["id"],
        project_id=row["project_id"],
        filename=row["filename"],
        size_bytes=row["size_bytes"],
        storage_uri=row["storage_uri"],
        status=DocumentStatus(row["status"]),
        phase=row["phase"],
        phase_current=row["phase_current"],
        phase_total=row["phase_total"],
        ingest_doc_id=row["ingest_doc_id"],
        sha256=row["sha256"],
        chunks_indexed=row["chunks_indexed"],
        skipped=None if row["skipped"] is None else bool(row["skipped"]),
        failure_reason=row["failure_reason"],
        failure_code=row["failure_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_document(
    conn: sqlite3.Connection,
    project_id: str,
    filename: str,
    size_bytes: int,
    storage_uri: str,
    status: DocumentStatus = DocumentStatus.pending,
    phase: str | None = "queued",
) -> DocumentRecord:
    """Insert a document. A `pending` upload starts life `phase='queued'` (the
    background ingest job overwrites the phase as it progresses)."""
    now = _now()
    document_id = _new_id()
    conn.execute(
        """
        INSERT INTO documents
            (id, project_id, filename, size_bytes, storage_uri, status, phase,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            project_id,
            filename,
            size_bytes,
            storage_uri,
            status.value,
            phase,
            now,
            now,
        ),
    )
    conn.commit()
    return get_document(conn, project_id, document_id)  # type: ignore[return-value]


def list_documents(conn: sqlite3.Connection, project_id: str) -> list[DocumentRecord]:
    rows = conn.execute(
        "SELECT * FROM documents WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return [_row_to_document(row) for row in rows]


def get_document(
    conn: sqlite3.Connection, project_id: str, document_id: str
) -> DocumentRecord | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ? AND project_id = ?",
        (document_id, project_id),
    ).fetchone()
    return None if row is None else _row_to_document(row)


def update_document_phase(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    phase: str,
    phase_current: int | None = None,
    phase_total: int | None = None,
) -> None:
    """Record the live ingestion step for a still-`pending` document.

    Called from the background ingest job as each SSE phase arrives; the status
    stays `pending` and only the progress columns move. `phase_current`/
    `phase_total` are the embedding chunk counters (NULL for other phases)."""
    conn.execute(
        """
        UPDATE documents
        SET phase = ?, phase_current = ?, phase_total = ?, updated_at = ?
        WHERE id = ?
        """,
        (phase, phase_current, phase_total, _now(), document_id),
    )
    conn.commit()


def update_document_result(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    status: DocumentStatus,
    ingest_doc_id: str | None = None,
    sha256: str | None = None,
    chunks_indexed: int | None = None,
    skipped: bool | None = None,
    failure_reason: str | None = None,
    failure_code: int | None = None,
) -> None:
    """Write the TERMINAL result and clear the live-progress columns.

    Any terminal status (indexed/skipped/failed) means ingestion is done, so
    `phase`/`phase_current`/`phase_total` are reset to NULL in the same write."""
    conn.execute(
        """
        UPDATE documents
        SET status = ?, phase = NULL, phase_current = NULL, phase_total = NULL,
            ingest_doc_id = ?, sha256 = ?, chunks_indexed = ?,
            skipped = ?, failure_reason = ?, failure_code = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            status.value,
            ingest_doc_id,
            sha256,
            chunks_indexed,
            None if skipped is None else int(skipped),
            failure_reason,
            failure_code,
            _now(),
            document_id,
        ),
    )
    conn.commit()


def delete_document(
    conn: sqlite3.Connection, project_id: str, document_id: str
) -> bool:
    cursor = conn.execute(
        "DELETE FROM documents WHERE id = ? AND project_id = ?",
        (document_id, project_id),
    )
    conn.commit()
    return cursor.rowcount > 0
