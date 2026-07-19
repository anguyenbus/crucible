"""The async upload's background job (`_run_ingest_job`) in isolation.

The endpoint returns 202 immediately (covered in test_upload_bridge); here we
drive the background worker directly with a known document id so we can peek the
DB WHILE ingestion streams — proving that a concurrent poll sees the live phase,
that embedding counters (`phase_current`/`phase_total`) land, and that every
terminal path (done → indexed/skipped, typed error → failed, unexpected
exception → failed) clears the phase columns. Ingestion is faked; no network.
"""

import pytest

from app import ingest_client, store
from app.api import documents as documents_api
from app.config import get_settings
from app.db import connect
from app.schemas import DocumentStatus


@pytest.fixture
def project_id(client):
    return client.post("/projects", json={"name": "Async"}).json()["id"]


@pytest.fixture
def pending_doc(client, project_id):
    """Create a real `pending`/`queued` document row and return (id, index)."""
    conn = connect(get_settings().db_path)
    try:
        doc = store.create_document(
            conn,
            project_id=project_id,
            filename="doc.md",
            size_bytes=10,
            storage_uri="/tmp/doc.md",
            status=DocumentStatus.pending,
            phase="queued",
        )
    finally:
        conn.close()
    return doc.id, f"proj-{project_id}"


def _run_job(document_id, *, on_stream):
    """Invoke the background job with a fake `ingest_stream` implementation."""
    monkey_target = documents_api.ingest_client
    original = monkey_target.ingest_stream
    monkey_target.ingest_stream = on_stream
    try:
        documents_api._run_ingest_job(
            document_id=document_id,
            storage_uri="/tmp/doc.md",
            index_name="proj-x",
            ingestion_url="http://ingestion.test",
            db_path=get_settings().db_path,
        )
    finally:
        monkey_target.ingest_stream = original


def _read(project_id, document_id):
    conn = connect(get_settings().db_path)
    try:
        return store.get_document(conn, project_id, document_id)
    finally:
        conn.close()


def test_job_records_live_phase_and_counters_then_clears_on_done(
    client, project_id, pending_doc
):
    document_id, _ = pending_doc
    snapshots = []

    def fake_stream(source, ingestion_url, index=None, *, on_phase):
        on_phase({"phase": "parsing"})
        snapshots.append(_read(project_id, document_id))
        on_phase({"phase": "embedding", "current": 3, "total": 8})
        snapshots.append(_read(project_id, document_id))
        return ingest_client.IngestResult("d1", "sha", 8, False)

    _run_job(document_id, on_stream=fake_stream)

    # Mid-flight polls saw the live phase (status stays pending throughout).
    assert snapshots[0].status == DocumentStatus.pending
    assert snapshots[0].phase == "parsing"
    assert snapshots[1].phase == "embedding"
    assert (snapshots[1].phase_current, snapshots[1].phase_total) == (3, 8)

    # Terminal: indexed, counters cleared.
    final = _read(project_id, document_id)
    assert final.status == DocumentStatus.indexed
    assert final.chunks_indexed == 8
    assert final.ingest_doc_id == "d1"
    assert final.phase is None
    assert final.phase_current is None and final.phase_total is None


def test_job_skip_result_persists_skipped(client, project_id, pending_doc):
    document_id, _ = pending_doc

    def fake_stream(source, ingestion_url, index=None, *, on_phase):
        return ingest_client.IngestResult("d1", "sha", 0, True)

    _run_job(document_id, on_stream=fake_stream)

    final = _read(project_id, document_id)
    assert final.status == DocumentStatus.skipped
    assert final.skipped is True
    assert final.phase is None


def test_job_typed_error_persists_failed_with_code(client, project_id, pending_doc):
    document_id, _ = pending_doc

    def fake_stream(source, ingestion_url, index=None, *, on_phase):
        on_phase({"phase": "parsing"})
        raise ingest_client.IngestionError(502, "embedding upstream down")

    _run_job(document_id, on_stream=fake_stream)

    final = _read(project_id, document_id)
    assert final.status == DocumentStatus.failed
    assert final.failure_code == 502
    assert "embedding upstream down" in final.failure_reason
    assert final.phase is None  # cleared even on failure


def test_job_unexpected_exception_still_fails_document(client, project_id, pending_doc):
    document_id, _ = pending_doc

    def fake_stream(source, ingestion_url, index=None, *, on_phase):
        raise RuntimeError("boom")  # not an IngestionError

    _run_job(document_id, on_stream=fake_stream)

    # A doc must never get stuck pending: an unexpected error → failed/500.
    final = _read(project_id, document_id)
    assert final.status == DocumentStatus.failed
    assert final.failure_code == 500
    assert "boom" in final.failure_reason
