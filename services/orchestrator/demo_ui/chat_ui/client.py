"""
HTTP glue to the orchestrator: the readyz gate and the streaming query call.

HTTP-only by design (``ORCHESTRATOR_URL``); SSE is consumed by parsing
``text/event-stream`` lines from ``httpx`` streamed responses directly via
:class:`chat_ui.sse.SSELineParser` — no ``httpx-sse``, no extra client.

Pre-stream failures (404 unknown config, 422 malformed ref, 502 dependency,
503 embed-throttle) arrive as plain HTTP errors BEFORE any SSE body starts;
they surface as :class:`QueryHTTPError` carrying the orchestrator's real
typed detail. Once the 200 is committed, failures arrive as exactly one SSE
``error`` event (handled by the caller, never here).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from chat_ui.sse import SSEEvent, SSELineParser

# Generation streams can be long; keep the read window generous but bounded.
_STREAM_TIMEOUT = httpx.Timeout(10.0, read=300.0)
_READYZ_TIMEOUT = httpx.Timeout(10.0)


class QueryHTTPError(Exception):
    """A pre-stream HTTP error from ``/query/stream`` (typed orchestrator detail)."""

    def __init__(self, status_code: int, detail: str) -> None:
        """Record the status code and the orchestrator's typed detail."""
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _error_detail(status_code: int, body: bytes) -> str:
    """Pull the orchestrator's real ``detail`` out of a JSON error body."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return f"HTTP {status_code} from the orchestrator."
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, list):
            # FastAPI/Pydantic 422 bodies carry a LIST of field errors (e.g. a
            # malformed pipeline_config or a bad history turn) — join the human
            # ``msg`` fields instead of showing a raw repr.
            messages = [str(item.get("msg", item)) for item in detail if item]
            if messages:
                return "; ".join(messages)
        elif detail:
            return str(detail)
    return f"HTTP {status_code} from the orchestrator."


def resolve_phoenix_project_gid(phoenix_endpoint: str | None, project_name: str) -> str | None:
    """
    Best-effort resolve the Phoenix project's GID (its GraphQL node id).

    Phoenix routes project pages by GID (e.g. ``UHJvamVjdDoy``), NOT by name,
    so the rendered trace link needs the GID to resolve. Called ONCE at UI
    startup; any failure (Phoenix unset/unreachable/old, unexpected shape)
    returns ``None`` and the caller falls back to the base Phoenix URL — the UI
    stays fully functional either way.
    """
    if not phoenix_endpoint:
        return None
    query = "{ projects(first: 100) { edges { node { id name } } } }"
    try:
        with httpx.Client(timeout=_READYZ_TIMEOUT) as client:
            response = client.post(f"{phoenix_endpoint.rstrip('/')}/graphql", json={"query": query})
        if response.status_code != 200:
            return None
        edges = response.json()["data"]["projects"]["edges"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    for edge in edges:
        node = edge.get("node", {})
        if node.get("name") == project_name:
            gid = node.get("id")
            return str(gid) if gid else None
    return None


async def check_ready(base_url: str) -> tuple[bool, str]:
    """
    Probe ``GET /readyz``; return ``(ready, detail)`` with the REAL error.

    Non-200 surfaces the orchestrator's own dependency detail (which
    dependency is down); an unreachable orchestrator surfaces the transport
    error. There is deliberately NO degraded fake mode behind this gate.
    """
    url = f"{base_url}/readyz"
    try:
        async with httpx.AsyncClient(timeout=_READYZ_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError as error:
        return False, f"orchestrator unreachable at {base_url} ({type(error).__name__}: {error})"
    if response.status_code == 200:
        return True, "ready"
    return False, _error_detail(response.status_code, response.content)


async def stream_query(
    base_url: str, payload: dict[str, Any], headers: dict[str, str]
) -> AsyncIterator[SSEEvent]:
    """
    POST ``/query/stream`` and yield typed SSE events as they arrive.

    ``headers`` carries the injected ``traceparent`` (empty under the no-op
    tracer). Raises :class:`QueryHTTPError` for pre-stream HTTP errors.
    """
    url = f"{base_url}/query/stream"
    async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise QueryHTTPError(
                    response.status_code, _error_detail(response.status_code, body)
                )
            parser = SSELineParser()
            async for line in response.aiter_lines():
                event = parser.feed(line)
                if event is not None:
                    yield event
