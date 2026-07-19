"""Task Group 2: SQLite store, integrity, and cascade (browser-free unit tests)."""

import sqlite3

import pytest

from app import store
from app.db import connect, init_db
from app.schemas import DocumentStatus


def test_init_db_is_idempotent(tmp_path):
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    init_db(db_path)  # re-run must be safe (create-if-absent)

    conn = connect(db_path)
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert {"projects", "documents"} <= tables


def test_project_and_document_round_trip(tmp_path):
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    conn = connect(db_path)

    project = store.create_project(conn, "Matter A")
    store.create_document(
        conn,
        project_id=project.id,
        filename="a.md",
        size_bytes=10,
        storage_uri="/tmp/a.md",
        status=DocumentStatus.indexed,
    )

    detail = store.get_project(conn, project.id)
    assert detail is not None
    assert detail.name == "Matter A"
    assert detail.document_count == 1
    listed = store.list_projects(conn)
    assert listed[0].document_count == 1
    docs = store.list_documents(conn, project.id)
    assert docs[0].filename == "a.md"
    conn.close()


def test_document_defaults_to_queued_phase(tmp_path):
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    conn = connect(db_path)
    try:
        project = store.create_project(conn, "P")
        doc = store.create_document(
            conn,
            project_id=project.id,
            filename="a.md",
            size_bytes=10,
            storage_uri="/tmp/a.md",
        )
        # A fresh pending upload starts life queued for the background job.
        assert doc.status == DocumentStatus.pending
        assert doc.phase == "queued"
        assert doc.phase_current is None and doc.phase_total is None
    finally:
        conn.close()


def test_update_phase_then_result_clears_progress(tmp_path):
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    conn = connect(db_path)
    try:
        project = store.create_project(conn, "P")
        doc = store.create_document(
            conn,
            project_id=project.id,
            filename="a.md",
            size_bytes=10,
            storage_uri="/tmp/a.md",
        )

        # Live embedding progress lands and is readable (what a poll would see).
        store.update_document_phase(
            conn, doc.id, phase="embedding", phase_current=3, phase_total=8
        )
        mid = store.get_document(conn, project.id, doc.id)
        assert mid.status == DocumentStatus.pending
        assert (mid.phase, mid.phase_current, mid.phase_total) == ("embedding", 3, 8)

        # The terminal write clears every progress column in the same update.
        store.update_document_result(
            conn,
            doc.id,
            status=DocumentStatus.indexed,
            ingest_doc_id="d",
            sha256="s",
            chunks_indexed=8,
            skipped=False,
        )
        final = store.get_document(conn, project.id, doc.id)
        assert final.status == DocumentStatus.indexed
        assert final.phase is None
        assert final.phase_current is None and final.phase_total is None
    finally:
        conn.close()


def test_unknown_phase_from_newer_ingestion_reads_without_error(tmp_path):
    """A phase this BFF version doesn't know must pass through, never 500 a read.

    Guards the cross-service contract: an independently-deployed ingestion could
    emit a new phase; the stored `phase` is typed `str`, so the document read
    tolerates it instead of failing Pydantic validation.
    """
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    conn = connect(db_path)
    try:
        project = store.create_project(conn, "P")
        doc = store.create_document(
            conn,
            project_id=project.id,
            filename="a.md",
            size_bytes=10,
            storage_uri="/tmp/a.md",
        )
        store.update_document_phase(conn, doc.id, phase="reranking")  # unknown here
        read = store.get_document(conn, project.id, doc.id)
        assert read.phase == "reranking"
        assert read.status == DocumentStatus.pending
    finally:
        conn.close()


def test_delete_project_cascades_documents(tmp_path):
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    conn = connect(db_path)

    project = store.create_project(conn, "Matter B")
    store.create_document(
        conn, project.id, "b.md", 5, "/tmp/b.md", DocumentStatus.pending
    )

    assert store.delete_project(conn, project.id) is True

    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE project_id = ?", (project.id,)
    ).fetchone()["n"]
    assert orphans == 0
    conn.close()


def test_project_id_is_indexed(tmp_path):
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    conn = connect(db_path)
    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    conn.close()
    assert "idx_documents_project_id" in indexes


def test_not_null_name_is_enforced_at_db_level(tmp_path):
    db_path = str(tmp_path / "webui.db")
    init_db(db_path)
    conn = connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("x", None, "t", "t"),
        )
    conn.close()
