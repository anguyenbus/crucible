"""Index lifecycle endpoints (project-scoped chat): provision + delete.

The BFF calls these over HTTP so index lifecycle stays with the service that
OWNS the index — the BFF holds no OpenSearch client. Both endpoints are
idempotent:
  - `POST /indices/{index_name}` provisions the knn index + hybrid-search
    pipeline; an existing index is a no-op. Explicit provisioning is REQUIRED
    before first ingest (dynamic auto-create would map `content_vector` as a
    plain float array, not a `knn_vector`).
  - `DELETE /indices/{index_name}` drops the index; a missing index is a
    benign no-op.

Error contract:
  400 — the caller-supplied index name is not an acceptable OpenSearch name
        (raised BEFORE any OpenSearch call)
  502 — OpenSearch upstream failure (safe, non-technical message)
"""

from fastapi import APIRouter, HTTPException
from opensearchpy.exceptions import OpenSearchException

from app.clients.index_admin import (
    InvalidIndexNameError,
    delete_index,
    provision_index,
)
from app.clients.opensearch import get_opensearch_client
from app.config import get_settings
from app.schemas.indices import DeleteIndexResponse, ProvisionIndexResponse

router = APIRouter()

_UPSTREAM_MESSAGE = "The search index is temporarily unavailable; please retry."


@router.post("/indices/{index_name}", response_model=ProvisionIndexResponse)
def provision(index_name: str) -> ProvisionIndexResponse:
    """Idempotently provision `index_name` (knn mapping + hybrid-search pipeline)."""
    settings = get_settings()
    try:
        created = provision_index(
            get_opensearch_client(),
            index_name,
            pipeline_name=settings.search_pipeline_name,
        )
    except InvalidIndexNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OpenSearchException as exc:
        raise HTTPException(status_code=502, detail={"message": _UPSTREAM_MESSAGE}) from exc
    return ProvisionIndexResponse(index=index_name, created=created)


@router.delete("/indices/{index_name}", response_model=DeleteIndexResponse)
def delete(index_name: str) -> DeleteIndexResponse:
    """Drop `index_name`; a missing index is a benign no-op (idempotent delete)."""
    try:
        deleted = delete_index(get_opensearch_client(), index_name)
    except InvalidIndexNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OpenSearchException as exc:
        raise HTTPException(status_code=502, detail={"message": _UPSTREAM_MESSAGE}) from exc
    return DeleteIndexResponse(index=index_name, deleted=deleted)
