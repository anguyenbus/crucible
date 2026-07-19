"""Task Group 3: projects & documents CRUD REST endpoints (TestClient, no auth).

Ingestion/S3 are not exercised here; documents are seeded via the store so the
read/delete surface can be tested without the upload bridge.
"""

from app import store
from app.db import connect
from app.schemas import DocumentStatus


def _seed_document(db_path: str, project_id: str, filename: str = "seed.md") -> str:
    conn = connect(db_path)
    doc = store.create_document(
        conn, project_id, filename, 12, "/tmp/seed.md", DocumentStatus.indexed
    )
    conn.close()
    return doc.id


def test_project_crud_lifecycle(client):
    created = client.post("/projects", json={"name": "Matter A"})
    assert created.status_code == 201
    project_id = created.json()["id"]
    assert created.json()["document_count"] == 0

    listed = client.get("/projects")
    assert listed.status_code == 200
    assert any(p["id"] == project_id for p in listed.json())

    renamed = client.patch(f"/projects/{project_id}", json={"name": "Matter A2"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Matter A2"

    detail = client.get(f"/projects/{project_id}")
    assert detail.status_code == 200
    assert detail.json()["documents"] == []

    deleted = client.delete(f"/projects/{project_id}")
    assert deleted.status_code == 204
    assert client.get(f"/projects/{project_id}").status_code == 404


def test_create_rejects_empty_name(client):
    assert client.post("/projects", json={"name": "   "}).status_code == 400


def test_unknown_project_is_404(client):
    assert client.get("/projects/nope").status_code == 404
    assert client.patch("/projects/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/projects/nope").status_code == 404


def test_delete_project_cascades_documents_via_api(client, settings_env):
    import os

    db_path = os.environ["WEBUI_DB_PATH"]
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]
    _seed_document(db_path, project_id)

    assert client.get(f"/projects/{project_id}").json()["document_count"] == 1
    assert client.delete(f"/projects/{project_id}").status_code == 204

    conn = connect(db_path)
    remaining = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    conn.close()
    assert remaining == 0


def test_documents_list_get_delete(client, settings_env):
    import os

    db_path = os.environ["WEBUI_DB_PATH"]
    project_id = client.post("/projects", json={"name": "P"}).json()["id"]
    doc_id = _seed_document(db_path, project_id)

    listed = client.get(f"/projects/{project_id}/documents")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == doc_id
    assert listed.json()[0]["status"] == "indexed"

    got = client.get(f"/projects/{project_id}/documents/{doc_id}")
    assert got.status_code == 200
    assert got.json()["status"] == "indexed"

    assert client.delete(f"/projects/{project_id}/documents/{doc_id}").status_code == 204
    assert client.get(f"/projects/{project_id}/documents/{doc_id}").status_code == 404


def test_document_not_in_project_is_404(client, settings_env):
    import os

    db_path = os.environ["WEBUI_DB_PATH"]
    project_a = client.post("/projects", json={"name": "A"}).json()["id"]
    project_b = client.post("/projects", json={"name": "B"}).json()["id"]
    doc_id = _seed_document(db_path, project_a)

    # Same document id, wrong project -> 404 (documents are project-scoped).
    assert client.get(f"/projects/{project_b}/documents/{doc_id}").status_code == 404
