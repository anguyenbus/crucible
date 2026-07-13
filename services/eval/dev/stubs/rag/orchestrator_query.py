"""
Orchestrator-backed RAG query callable (HTTP; dev/-only).

NOTE: This is a local-only dev/ adapter (never imported from app/). It drives
the orchestrator service's ``POST /query`` over HTTP — the one-way dependency
rule holds: eval talks to the orchestrator over HTTP ONLY, the orchestrator
never imports eval, and nothing under ``services/orchestrator`` is imported
here.

The whole adapter is a one-line unwrap: POST ``{question, pipeline_config}``
to ``{ORCHESTRATOR_URL}/query`` and return ``envelope["result"]`` — the eval
``rag_query_output`` v1.1.0 payload the orchestrator passes through verbatim
(``RagAdapter`` re-validates it against the packaged schema).

Config surface (env-only, matching the sibling ``opensearch_query`` precedence
conventions — there is no CLI/yaml layer for these):

- ``ORCHESTRATOR_URL``: base URL of a running orchestrator
  (default ``http://localhost:8000``).
- ``ORCHESTRATOR_PIPELINE_CONFIG``: fully-qualified pinned-config reference
  posted with every query (default ``legal-rag-default-1.1.0``). ``top_k``
  and ALL behavior pins live inside that pinned config — there is no
  per-request override (the CLI's ``--top-k`` is rejected loudly for this
  backend).

Failure contract: any non-200 response raises ``OrchestratorQueryError``
naming the HTTP status and body — the harness must never score a failed
query as a real (zero) result.

Documented stub-ism: the ``corpus_dir`` parameter of the RagAdapter callable
is accepted and IGNORED — retrieval happens inside the orchestrator against
its own OpenSearch index, not a local corpus.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import httpx

# Env-var surface (env-only; read at call time so a shell export between
# queries is honored, mirroring the sibling opensearch_query adapter).
ENV_URL: Final[str] = "ORCHESTRATOR_URL"
ENV_PIPELINE_CONFIG: Final[str] = "ORCHESTRATOR_PIPELINE_CONFIG"

DEFAULT_URL: Final[str] = "http://localhost:8000"
DEFAULT_PIPELINE_CONFIG: Final[str] = "legal-rag-default-1.1.0"

# One live query embeds, retrieves and generates; the orchestrator's internal
# Bedrock throttle retries can stretch a single call well past httpx's 5 s
# default (same rationale as the ingest driver's timeout).
REQUEST_TIMEOUT_S: Final[float] = 300.0


class OrchestratorQueryError(RuntimeError):
    """A non-200 response from the orchestrator's ``POST /query``."""

    def __init__(self, status_code: int, body: str) -> None:
        """Build the message from the HTTP status code and response body."""
        super().__init__(
            f"Orchestrator POST /query failed with HTTP {status_code}: {body} "
            "— refusing to score a failed query."
        )
        self.status_code = status_code
        self.body = body


def query(
    question: str,
    corpus_dir: Path | None = None,
    query_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """
    Query the orchestrator service over HTTP and unwrap its envelope.

    Args:
        question: User question to answer.
        corpus_dir: Accepted and IGNORED (documented stub-ism — the RagAdapter
            callable contract passes it, but retrieval happens server-side).
        query_id: Optional stable identifier forwarded to the orchestrator
            (echoed back in ``result.query.query_id``); omitted from the POST
            body when the harness does not supply one, in which case the
            orchestrator generates one.
        transport: Injectable httpx transport for tests (mocked HTTP only).

    Returns:
        The envelope's ``result``: an eval rag_query_output v1.1.0 payload.

    Raises:
        OrchestratorQueryError: On any non-200 response (status + body named).
        httpx.HTTPError: On transport-level failure (e.g. connection refused).

    """
    _ = corpus_dir  # Documented stub-ism: accepted and ignored.

    payload: dict[str, Any] = {
        "question": question,
        "pipeline_config": os.getenv(ENV_PIPELINE_CONFIG, DEFAULT_PIPELINE_CONFIG),
    }
    if query_id is not None:
        payload["query_id"] = query_id

    base_url = os.getenv(ENV_URL, DEFAULT_URL)
    with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
        response = client.post("/query", json=payload)

    if response.status_code != 200:
        raise OrchestratorQueryError(response.status_code, response.text)

    # The one-line envelope unwrap: result IS the rag_query_output payload.
    return response.json()["result"]


# Explicit alias matching the spec's naming of the RagAdapter callable.
orchestrator_query = query
