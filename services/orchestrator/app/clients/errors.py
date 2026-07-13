"""
Client exception taxonomy: the seams the app-level handlers map to HTTP.

Defined here so the router/handlers never need to inspect botocore or
opensearch-py internals to classify a failure:

- :class:`BedrockThrottleExhaustedError` → 503 with a ``Retry-After`` header
  and ``dependency: "bedrock"`` (throttling is transient; the caller should
  retry later).
- A NON-throttle botocore ``ClientError`` is re-raised UNCHANGED by
  ``app.clients.bedrock`` (its error code stays inspectable) → 502 with
  ``dependency: "bedrock"``.
- OpenSearch transport/search failures surface as opensearch-py exceptions
  → 502 with ``dependency: "opensearch"``.
- :class:`OpenSearchNotReadyError` → the service renders NOT-READY (the index
  mapping ``_meta`` contradicts the query-side embedder, so every retrieval
  would be garbage scored as a real result).

The HTTP mapping itself lives in app-level exception handlers (wired by the
pipeline-wiring task group), never in per-route try/except.
"""


class BedrockThrottleExhaustedError(Exception):
    """
    Bedrock throttling persisted past the bounded retry budget.

    Raised by ``app.clients.bedrock`` after capped exponential backoff ran
    out; the original throttling ``ClientError`` rides along as ``__cause__``.
    Mapped to HTTP 503 (+ ``Retry-After``) by the app-level handler.
    """


class OpenSearchNotReadyError(Exception):
    """
    The index mapping ``_meta`` mismatches the query-side embedder.

    Raised at OpenSearch client init by the verify-IF-PRESENT ``_meta`` guard
    (absent ``_meta`` → trust config, per docs/byo-index-contract.md).
    Renders the service not-ready — querying with a mismatched embedder
    produces garbage retrieval scored as a real result.
    """
