"""
``text/event-stream`` framing parser over httpx-streamed lines.

Deliberately dependency-free (stdlib only): the orchestrator's SSE contract
is three typed events (``token``* then exactly one ``final`` | ``error``),
each a single JSON ``data:`` payload — a full SSE client library
(``httpx-sse`` etc.) is excluded by decision. Lines come from
``httpx.Response.aiter_lines()`` (line endings already stripped); the parser
is incremental (``feed`` one line at a time) so it wraps sync and async line
sources alike.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SSEEvent:
    """One parsed SSE event: the event name plus its decoded JSON data."""

    event: str
    data: dict[str, Any]


class SSELineParser:
    """
    Incremental SSE parser: ``feed`` each line, get an event on frame end.

    Framing per the SSE spec subset the orchestrator emits: ``event:`` and
    ``data:`` field lines, dispatched by a blank line. Multiple ``data:``
    lines are joined with newlines before JSON decoding; comment lines
    (leading ``:``) are ignored; an event with no data is dropped.
    """

    def __init__(self) -> None:
        """Start with an empty frame accumulator."""
        self._event: str = ""
        self._data_lines: list[str] = []

    def feed(self, line: str) -> SSEEvent | None:
        """Consume one stream line; return the completed event, if any."""
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # SSE comment (keep-alive) — ignored
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            self._event = value
        elif field == "data":
            self._data_lines.append(value)
        # Other SSE fields (id, retry) are not part of the contract — ignored.
        return None

    def _dispatch(self) -> SSEEvent | None:
        """Build the event at a frame boundary and reset the accumulator."""
        event_name, data_lines = self._event, self._data_lines
        self._event, self._data_lines = "", []
        if not data_lines:
            return None
        return SSEEvent(event=event_name or "message", data=json.loads("\n".join(data_lines)))


def parse_sse_lines(lines: list[str]) -> list[SSEEvent]:
    """Parse a complete list of stream lines (test/offline convenience)."""
    parser = SSELineParser()
    events = [event for line in lines if (event := parser.feed(line)) is not None]
    # A final frame not terminated by a blank line still dispatches.
    trailing = parser.feed("")
    if trailing is not None:
        events.append(trailing)
    return events
