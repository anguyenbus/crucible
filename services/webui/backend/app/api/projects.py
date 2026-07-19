"""Projects CRUD REST surface (no auth).

Shaped after donna's `routes/projects.py` MINUS all auth/user scoping and MINUS
folders/people/chats/tabular-reviews: no `user_id`, `visibility`, `shared_with`,
`is_owner`, or access checks -- that surface is stripped, not stubbed.

Per-project index lifecycle (project-scoped chat) is driven over HTTP to
ingestion (the BFF holds no OpenSearch client): a project's `proj-{id}` index is
provisioned on create and dropped on delete. Provisioning is idempotent; a
create whose provision fails rolls the project row back so there is no orphan
project without an index.
"""

from sqlite3 import Connection

from fastapi import APIRouter, Depends, HTTPException, status

from app import index_client, store
from app.config import get_settings
from app.deps import get_conn
from app.schemas import ProjectCreate, ProjectDetail, ProjectSummary, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])


def _require_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    return trimmed


@router.post("", response_model=ProjectSummary, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate, conn: Connection = Depends(get_conn)
) -> ProjectSummary:
    project = store.create_project(conn, _require_name(body.name))
    # Provision the per-project index up front (idempotent). If ingestion is
    # unreachable, roll the project row back so we never leave a project without
    # its index — the caller sees an honest 502, not a half-created project.
    try:
        index_client.provision(project.index_name, get_settings().ingestion_url)
    except index_client.IndexServiceError as exc:
        store.delete_project(conn, project.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to provision project index: {exc.message}",
        ) from exc
    return project


@router.get("", response_model=list[ProjectSummary])
def list_projects(conn: Connection = Depends(get_conn)) -> list[ProjectSummary]:
    return store.list_projects(conn)


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(
    project_id: str, conn: Connection = Depends(get_conn)
) -> ProjectDetail:
    project = store.get_project(conn, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return project


@router.patch("/{project_id}", response_model=ProjectDetail)
def rename_project(
    project_id: str, body: ProjectUpdate, conn: Connection = Depends(get_conn)
) -> ProjectDetail:
    project = store.rename_project(conn, project_id, _require_name(body.name))
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, conn: Connection = Depends(get_conn)) -> None:
    project = store.get_project(conn, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        )
    # Drop the project's index FIRST (idempotent; missing index is a no-op) so a
    # transient failure leaves the metadata intact and the delete is retry-safe;
    # only then cascade the document rows. This removes the chunks, not just the
    # metadata (fixes the parked "index cleanup on delete" caveat).
    try:
        index_client.delete(project.index_name, get_settings().ingestion_url)
    except index_client.IndexServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to drop project index: {exc.message}",
        ) from exc
    store.delete_project(conn, project_id)
