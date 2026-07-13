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

import asyncio
import contextlib
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

    The httpx request runs in a DEDICATED pump task, so the ``AsyncClient`` and
    the streamed response are opened AND closed entirely within one task. httpx
    (via anyio) binds its connection-pool cancel scopes to the task that entered
    them; closing them from a DIFFERENT task raises "Attempted to exit cancel
    scope in a different task". That is exactly what happened when this
    generator — holding the httpx ``async with`` open across ``yield`` — was
    ``aclose()``d by a consumer that broke early (on the terminal ``final``
    event) under Chainlit, which can resume/close a coroutine on another task.
    The pump hands events to this generator over a queue; on early break the
    generator's ``finally`` cancels the pump, whose httpx teardown then runs in
    the pump's OWN task — no cross-task cancel scope. Pair with
    ``contextlib.aclosing`` on the consumer so this ``finally`` runs in the loop
    (not deferred to GC, which cannot await the cancel).
    """
    url = f"{base_url}/query/stream"
    # Unbounded: an SSE response is bounded, and never awaiting on ``put`` keeps
    # the pump's ``finally`` (the sentinel) safe to run during cancellation.
    queue: asyncio.Queue[SSEEvent | Exception | None] = asyncio.Queue()

    async def _pump() -> None:
        """Own the httpx lifecycle in one task; feed events/errors to the queue."""
        try:
            async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        queue.put_nowait(
                            QueryHTTPError(
                                response.status_code,
                                _error_detail(response.status_code, body),
                            )
                        )
                        return
                    parser = SSELineParser()
                    async for line in response.aiter_lines():
                        event = parser.feed(line)
                        if event is not None:
                            queue.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — hand transport failures to the consumer
            queue.put_nowait(exc)
        finally:
            queue.put_nowait(None)  # terminal sentinel (also runs on cancel)

    pump = asyncio.create_task(_pump())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
