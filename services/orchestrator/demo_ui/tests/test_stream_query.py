"""
stream_query: pump-task isolation of the httpx lifecycle.

The SSE request runs in a dedicated task so httpx's task-bound cancel scopes are
opened and closed in the SAME task even when the consumer breaks early (the
terminal `final` event) and aclose()s this generator from a Chainlit task —
otherwise httpx raises "Attempted to exit cancel scope in a different task".
Driven with an httpx MockTransport (no network), consumed exactly like
on_message (aclosing + break on `final`).
"""

import asyncio
import json
from contextlib import aclosing

import httpx
from chat_ui import client as client_mod
from chat_ui.client import QueryHTTPError, stream_query

_SSE_BODY = (
    b'event: token\ndata: {"text": "The supply is"}\n\n'
    b'event: token\ndata: {"text": " GST-free"}\n\n'
    b'event: final\ndata: {"result": {"answer": {"text": "x"}}}\n\n'
)


def _mock_httpx_async_client(monkeypatch, handler) -> None:
    """Make stream_query's ``httpx.AsyncClient(...)`` use a MockTransport."""
    real_async_client = httpx.AsyncClient

    def factory(**_kwargs):
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", factory)


def test_stream_query_yields_events_then_cleans_up_on_break(monkeypatch):
    def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_SSE_BODY)

    _mock_httpx_async_client(monkeypatch, handler)

    async def _run():
        seen = []
        # The on_message consumption pattern: aclosing + break on `final`.
        async with aclosing(stream_query("http://orch", {"question": "q"}, {})) as events:
            async for event in events:
                seen.append((event.event, event.data))
                if event.event == "final":
                    break
        return seen

    seen = asyncio.run(_run())
    assert [name for name, _ in seen] == ["token", "token", "final"]
    assert seen[0][1] == {"text": "The supply is"}
    # Reaching here without an "exit cancel scope in a different task" / ignored
    # GeneratorExit error IS the assertion: the pump tore down httpx cleanly.


def test_stream_query_raises_typed_error_on_pre_stream_non_200(monkeypatch):
    def handler(_request):
        return httpx.Response(404, json={"detail": "Unknown pipeline_config reference."})

    _mock_httpx_async_client(monkeypatch, handler)

    async def _run():
        async with aclosing(stream_query("http://orch", {}, {})) as events:
            async for _event in events:  # pragma: no cover - should raise before yielding
                pass

    try:
        asyncio.run(_run())
        raised = None
    except QueryHTTPError as err:
        raised = err

    assert raised is not None
    assert raised.status_code == 404
    assert "Unknown pipeline_config" in raised.detail
    # Sanity: the body-detail extraction still works through the pump path.
    assert json.loads(json.dumps(raised.detail))  # detail is a plain string
