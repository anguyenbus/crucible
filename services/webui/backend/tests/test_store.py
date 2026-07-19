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
