"""HTTP client for ingestion's index endpoints (provision + delete + read chunks).

The BFF NEVER holds an OpenSearch client and NEVER imports the
`services.ingestion` package — ALL per-project index work is an HTTP call to
ingestion (which owns the index), consumed over `WEBUI_INGESTION_URL`. This
keeps the dependency firewall intact.

  - `provision(index_name, url)` → `POST {url}/indices/{index_name}` (idempotent)
  - `delete(index_name, url)`    → `DELETE {url}/indices/{index_name}` (idempotent)
  - `get_document_chunks(index_name, doc_id, url)`
        → `GET {url}/indices/{index_name}/documents/{doc_id}/chunks` (read text)

A non-2xx response or a network fault is normalized to a typed
`IndexServiceError` so callers can surface an honest 502 rather than an opaque
hang or 500.
"""

from typing import Any

import httpx

INDEX_TIMEOUT_SECONDS = 30.0


class IndexServiceError(Exception):
    """A typed index-lifecycle failure carrying the HTTP-equivalent code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _url(ingestion_url: str, index_name: str) -> str:
    return f"{ingestion_url.rstrip('/')}/indices/{index_name}"


def provision(index_name: str, ingestion_url: str) -> bool:
    """Provision `index_name` via ingestion (idempotent); return `created`."""
    url = _url(ingestion_url, index_name)
    try:
        response = httpx.post(url, timeout=INDEX_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise IndexServiceError(502, f"ingestion unreachable at {url}: {exc}") from exc
    if response.status_code == 200:
        return bool(response.json().get("created", False))
    raise IndexServiceError(response.status_code, _detail(response))


def delete(index_name: str, ingestion_url: str) -> bool:
    """Delete `index_name` via ingestion (idempotent); return `deleted`."""
    url = _url(ingestion_url, index_name)
    try:
        response = httpx.request("DELETE", url, timeout=INDEX_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise IndexServiceError(502, f"ingestion unreachable at {url}: {exc}") from exc
    if response.status_code == 200:
        return bool(response.json().get("deleted", False))
    raise IndexServiceError(response.status_code, _detail(response))


def get_document_chunks(
    index_name: str, doc_id: str, ingestion_url: str
) -> dict[str, Any]:
    """Fetch one document's indexed chunks/text from ingestion (the index owner).

    Returns the parsed body `{index, doc_id, chunk_count, text, chunks}`. A
    non-2xx or network fault is normalized to `IndexServiceError` so the caller
    surfaces an honest status rather than a swallowed 500.
    """
    url = (
        f"{ingestion_url.rstrip('/')}/indices/{index_name}"
        f"/documents/{doc_id}/chunks"
    )
    try:
        response = httpx.get(url, timeout=INDEX_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise IndexServiceError(502, f"ingestion unreachable at {url}: {exc}") from exc
    if response.status_code == 200:
        return response.json()
    raise IndexServiceError(response.status_code, _detail(response))


def delete_document_chunks(index_name: str, doc_id: str, ingestion_url: str) -> int:
    """Delete one document's chunks from `index_name` via ingestion (idempotent).

    Returns the number of chunks deleted. A non-2xx or network fault is
    normalized to `IndexServiceError` so the caller can refuse to drop the
    document row when the index cleanup did not succeed.
    """
    url = f"{ingestion_url.rstrip('/')}/indices/{index_name}/documents/{doc_id}"
    try:
        response = httpx.request("DELETE", url, timeout=INDEX_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise IndexServiceError(502, f"ingestion unreachable at {url}: {exc}") from exc
    if response.status_code == 200:
        return int(response.json().get("deleted", 0))
    raise IndexServiceError(response.status_code, _detail(response))


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"ingestion returned HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if isinstance(detail, str):
        return detail
    return str(body)
