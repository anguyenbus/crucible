"""
Read-only OpenSearch search client (BYO index contract v2).

Constructed ONCE in FastAPI lifespan from ``Settings`` location facts and
injected into the pipeline by the router — pipeline stages receive the
instance and never import opensearch-py themselves (``stages-pure``
import-linter contract).

Surface is STRICTLY read-only: ``search`` (with the hybrid search pipeline
bound explicitly PER REQUEST as a query parameter — never
``index.search.default_pipeline``), ``index_exists`` (for readyz), and one
mapping fetch at init for the ``_meta`` guard. No ingest, no index mutation,
and NO OpenSearch retries anywhere (``max_retries=0``).

Auth is SigV4 (service ``"es"``) via the ambient boto3 credential chain with
a 60s timeout — the same construction shape as eval's
``dev/stubs/rag/opensearch_query.py`` (pattern parity only; eval code is
never imported).

``search`` takes an optional per-request ``index`` OVERRIDE (project-scoped
chat): ``None`` keeps the lifespan-bound index (today's single
``legal-rag-bench``), so absent-field behavior is byte-identical; a supplied
string (OpenSearch's native comma-separated multi-index) scopes THIS request
only. The index is a LOCATION fact, never a behavior pin.

``_meta`` guard (verify-IF-PRESENT, per docs/byo-index-contract.md): an index
without a mapping ``_meta`` block is trusted as configured (the POC
``legal-rag-bench`` index has none); a ``_meta`` block whose ``embedder`` or
``dims`` contradicts the query-side embedder raises
:class:`OpenSearchNotReadyError` so the service renders not-ready instead of
scoring garbage retrieval as a real result. The one-shot guard runs against
the lifespan-bound index only; ingestion-built ``proj-{id}`` indices are
Titan-v2-1024-compatible by construction (same pinned embedder), so extending
the guard to per-request project indices is deliberately DEFERRED.
"""

from __future__ import annotations

from typing import Any, Final

from app.clients.errors import OpenSearchNotReadyError

_TIMEOUT_SECONDS: Final[int] = 60


def _create_raw_client(endpoint: str, *, region: str) -> Any:
    """
    Build the SigV4-authenticated opensearch-py client (no retries).

    Imports live inside the function so importing this module stays cheap;
    tests monkeypatch ``opensearchpy.OpenSearch``/``boto3.Session`` to
    inspect construction without any network access.
    """
    import boto3
    from opensearchpy import OpenSearch, Urllib3AWSV4SignerAuth, Urllib3HttpConnection

    host = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    credentials = boto3.Session().get_credentials()
    auth = Urllib3AWSV4SignerAuth(credentials, region, "es")

    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=Urllib3HttpConnection,
        timeout=_TIMEOUT_SECONDS,
        # No OpenSearch retries anywhere: a failed search maps straight to
        # 502 (dependency: opensearch); only Bedrock throttling is retried.
        max_retries=0,
        retry_on_timeout=False,
    )


def verify_index_meta(
    mapping_response: dict[str, Any],
    *,
    embedder_model: str,
    embedder_dimensions: int,
) -> None:
    """
    Verify the index mapping ``_meta`` block against the query-side embedder.

    Verify-IF-PRESENT: absent ``_meta`` is a complete no-op (trust the
    config, per the BYO contract); a present block is checked key-by-key.

    Args:
        mapping_response: ``client.indices.get_mapping(index=...)`` response
            (``{index_name: {"mappings": {...}}}``).
        embedder_model: Query-side embedder model id (config pin).
        embedder_dimensions: Query-side embedding dimension.

    Raises:
        OpenSearchNotReadyError: On an embedder model or dimension mismatch.

    """
    for index_name, index_body in mapping_response.items():
        meta = index_body.get("mappings", {}).get("_meta")
        if not meta:
            continue

        indexed_model = meta.get("embedder")
        if indexed_model is not None and indexed_model != embedder_model:
            raise OpenSearchNotReadyError(
                f"Embedder mismatch for index '{index_name}': index _meta "
                f"declares '{indexed_model}' but the query-side embedder is "
                f"'{embedder_model}'. Querying with a mismatched embedder "
                "produces garbage retrieval; fix the embedder (or the index)."
            )

        indexed_dims = meta.get("dims")
        if indexed_dims is not None and int(indexed_dims) != int(embedder_dimensions):
            raise OpenSearchNotReadyError(
                f"Embedding-dimension mismatch for index '{index_name}': "
                f"index _meta declares {indexed_dims} dims but the query-side "
                f"embedder produces {embedder_dimensions}."
            )


class OpenSearchSearchClient:
    """
    Strictly read-only search surface over one endpoint + index.

    Public surface is ``search`` and ``index_exists`` ONLY — no ingest or
    index-mutation capability exists on this class by design.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        index: str,
        embedder_model_id: str,
        embedder_dimensions: int,
        region: str,
        raw_client: Any | None = None,
    ) -> None:
        """
        Build the client and run the ``_meta`` guard ONCE.

        Args:
            endpoint: OpenSearch domain endpoint (Settings location fact).
            index: Target index name (Settings location fact).
            embedder_model_id: Query-side embedder model id (config pin),
                checked against the index ``_meta`` when present.
            embedder_dimensions: Query-side embedding dimension.
            region: AWS region for SigV4 signing.
            raw_client: Optional pre-built opensearch-py client (tests inject
                a fake); default builds the SigV4 client.

        Raises:
            OpenSearchNotReadyError: ``_meta`` present and mismatched — the
                service must render not-ready.

        """
        self._index = index
        self._raw = (
            raw_client if raw_client is not None else _create_raw_client(endpoint, region=region)
        )
        mapping = self._raw.indices.get_mapping(index=index)
        verify_index_meta(
            mapping,
            embedder_model=embedder_model_id,
            embedder_dimensions=embedder_dimensions,
        )

    def search(
        self, body: dict[str, Any], *, search_pipeline: str, index: str | None = None
    ) -> dict[str, Any]:
        """
        Run one search with the hybrid pipeline bound PER REQUEST.

        Args:
            body: OpenSearch query body (built by the pure retriever stage).
            search_pipeline: Search-pipeline name, passed explicitly as the
                ``search_pipeline`` query parameter on THIS request — never
                relied on via ``index.search.default_pipeline``.
            index: Optional per-request index scope (project-scoped chat). A
                comma-separated OpenSearch multi-index string; ``None`` (the
                eval/default) falls back to the lifespan-bound index so
                absent-field behavior is byte-identical to before.

        Returns:
            The raw OpenSearch search response.

        """
        return self._raw.search(
            index=index if index is not None else self._index,
            body=body,
            params={"search_pipeline": search_pipeline},
        )

    def index_exists(self) -> bool:
        """Report whether the configured index exists (readyz probe)."""
        return bool(self._raw.indices.exists(index=self._index))
