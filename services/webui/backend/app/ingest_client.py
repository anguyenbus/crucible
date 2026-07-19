"""HTTP client for the ingestion service's synchronous `POST /ingest`.

The BFF consumes ingestion over its env-var URL ONLY (`WEBUI_INGESTION_URL`) --
it never imports the `services.ingestion` package. Ingestion is SYNCHRONOUS, so
this call BLOCKS until ingestion returns; there is no job queue and no fabricated
progress.

Ingestion's typed error surface (per `services/ingestion/README.md`) is mapped
to `IngestionError` carrying the HTTP-equivalent code and a message, so the
upload endpoint can persist a structured `failed` document status instead of
swallowing a 500:
    400 — invalid/unsupported source, or over `max_chunks_per_doc`
    404 — missing S3 object / local file
    502 — Bedrock/OpenSearch upstream failure
A network fault reaching ingestion (killed/unreachable) is normalized to a 502
so a killed ingestion yields a typed, persisted, renderable failure -- never a
hang or an opaque error.
"""

import httpx

INGEST_TIMEOUT_SECONDS = 120.0


class IngestResult:
    def __init__(self, doc_id: str, sha256: str, chunks_indexed: int, skipped: bool):
        self.doc_id = doc_id
        self.sha256 = sha256
        self.chunks_indexed = chunks_indexed
        self.skipped = skipped


class IngestionError(Exception):
    """A typed ingestion-domain failure carrying the HTTP-equivalent code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _extract_detail(response: httpx.Response) -> str:
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


def ingest(
    source: str, ingestion_url: str, index: str | None = None
) -> IngestResult:
    """Call ingestion synchronously; raise `IngestionError` on any failure.

    `index` (project-scoped chat) targets a specific per-project OpenSearch
    index; when omitted, ingestion writes to its service-default index.
    """
    url = f"{ingestion_url.rstrip('/')}/ingest"
    payload: dict[str, str] = {"source": source}
    if index is not None:
        payload["index"] = index
    try:
        response = httpx.post(url, json=payload, timeout=INGEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise IngestionError(502, f"ingestion unreachable at {url}: {exc}") from exc

    if response.status_code == 200:
        body = response.json()
        return IngestResult(
            doc_id=body["doc_id"],
            sha256=body["sha256"],
            chunks_indexed=body["chunks_indexed"],
            skipped=body["skipped"],
        )

    raise IngestionError(response.status_code, _extract_detail(response))
