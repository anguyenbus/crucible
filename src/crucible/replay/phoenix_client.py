"""
Phoenix client wrapper for querying synthetic spans.

This module provides a wrapper around Phoenix's SpanQuery functionality
for querying and extracting synthetic spans for replay evaluation.
"""

from __future__ import annotations

from beartype import beartype
from beartype.typing import Any, Final

# Constants
DEFAULT_ENDPOINT: Final[str] = "http://localhost:6006"
DEFAULT_PROJECT_NAME: Final[str] = "case-assistant-synthetic"


@beartype
class PhoenixClient:
    """
    Phoenix client for querying synthetic spans.

    Provides methods to query root spans with synthetic markers
    and extract child spans by kind.

    Attributes:
        _endpoint: Phoenix server endpoint.
        _project_name: Phoenix project name.
        _client: Phoenix client instance (None if not connected).

    Example:
        >>> client = PhoenixClient()
        >>> root_spans = client.query_root_spans(limit=10)
        >>> for span in root_spans:
        ...     children = client.extract_child_spans(span)

    """

    __slots__ = ("_endpoint", "_project_name", "_client")

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        project_name: str = DEFAULT_PROJECT_NAME,
    ) -> None:
        """
        Initialize Phoenix client.

        Args:
            endpoint: Phoenix server endpoint.
            project_name: Phoenix project name.

        """
        self._endpoint = endpoint
        self._project_name = project_name
        self._client = self._initialize_client()

    def _initialize_client(self) -> Any | None:
        """
        Initialize Phoenix client.

        Returns:
            Phoenix client instance or None if initialization fails.

        """
        try:
            # Try to import from new phoenix package structure
            from phoenix.client import Client as PhoenixClientClass

            return PhoenixClientClass(base_url=self._endpoint)
        except (ImportError, AttributeError):
            try:
                # Try legacy import path
                from arize_phoenix.client import Client as PhoenixClientClass

                # Legacy might use endpoint or base_url
                try:
                    return PhoenixClientClass(base_url=self._endpoint)
                except TypeError:
                    return PhoenixClientClass(endpoint=self._endpoint)
            except (ImportError, AttributeError):
                # Phoenix not available - return None
                return None

    @beartype
    def is_connected(self) -> bool:
        """
        Check if Phoenix client is connected.

        Returns:
            True if connected, False otherwise.

        """
        return self._client is not None

    @beartype
    def query_root_spans(
        self,
        limit: int = 100,
        case_id: str | None = None,
        tenant_id_hashed: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query root spans with synthetic marker.

        Filters for parent_id is None and eval_harness.synthetic == True.

        Args:
            limit: Maximum number of spans to return.
            case_id: Optional filter by case_id.
            tenant_id_hashed: Optional filter by tenant_id_hashed.

        Returns:
            List of root span dictionaries.

        Raises:
            ConnectionError: If Phoenix client is not connected.

        """
        if not self.is_connected():
            raise ConnectionError("Phoenix client is not connected")

        # Query spans using Phoenix client
        try:
            spans_df = self._client.spans.get_spans_dataframe(
                project_name=self._project_name
            )
        except Exception:
            return []

        # Filter for root spans (parent_id is NaN or empty)
        root_mask = spans_df["parent_id"].isna() | (spans_df["parent_id"] == "")
        root_spans = spans_df[root_mask]

        # Filter for synthetic spans by name (synthetic_rag_query)
        # Note: Using span name instead of synthetic marker because nested
        # eval_harness.synthetic attribute doesn't filter correctly in DataFrame
        if "name" in root_spans.columns:
            root_spans = root_spans[
                root_spans["name"] == "synthetic_rag_query"
            ]

        # Apply additional filters
        if case_id is not None:
            case_col = "attributes.eval_harness.case_id"
            if case_col in root_spans.columns:
                root_spans = root_spans[root_spans[case_col] == case_id]

        if tenant_id_hashed is not None:
            tenant_col = "attributes.eval_harness.tenant_id_hashed"
            if tenant_col in root_spans.columns:
                root_spans = root_spans[root_spans[tenant_col] == tenant_id_hashed]

        # Convert to list of dicts and limit
        return root_spans.head(limit).to_dict("records")

    @beartype
    def extract_child_spans(
        self,
        parent_span: dict[str, Any],
        span_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Extract child spans from a parent span.

        Args:
            parent_span: Parent span dictionary.
            span_kind: Optional filter by span_kind (EMBEDDING, RETRIEVER, LLM).

        Returns:
            List of child span dictionaries.

        """
        if not self.is_connected():
            return []

        parent_span_id = parent_span.get("span_id") or parent_span.get("id")
        if not parent_span_id:
            return []

        try:
            # Try to use query_spans method
            if hasattr(self._client, "query_spans"):
                filters = {"parent_id": parent_span_id}
                if span_kind is not None:
                    filters["span_kind"] = span_kind.upper()
                return self._client.query_spans(filters, limit=100)
            # Try to use spans() method
            elif hasattr(self._client, "spans"):
                spans_df = self._client.spans(project_name=self._project_name)
                # Filter for child spans
                child_spans = spans_df[spans_df.get("parent_id") == parent_span_id]
                # Filter by span_kind if specified
                if span_kind is not None:
                    child_spans = child_spans[
                        child_spans.get("span_kind").str.upper() == span_kind.upper()
                    ]
                return child_spans.to_dict("records")
            else:
                return []
        except Exception:
            return []
