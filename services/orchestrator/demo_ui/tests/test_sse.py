"""SSE line-parser tests: text/event-stream framing over httpx-style lines."""

from chat_ui.sse import SSELineParser, parse_sse_lines


def test_parser_yields_typed_token_then_final_events_in_order():
    lines = [
        ": keep-alive comment is ignored",
        "event: token",
        'data: {"text": "Hello"}',
        "",
        "event: token",
        'data: {"text": " world [c:1]"}',
        "",
        "event: final",
        'data: {"result": {"answer": {"text": "Hello world [c:1]"}}}',
        "",
    ]
    events = parse_sse_lines(lines)

    assert [event.event for event in events] == ["token", "token", "final"]
    assert events[0].data == {"text": "Hello"}
    assert events[1].data == {"text": " world [c:1]"}  # raw delta, marker unresolved
    assert events[2].data["result"]["answer"]["text"] == "Hello world [c:1]"


def test_parser_error_event_framing_and_incremental_feed():
    parser = SSELineParser()
    assert parser.feed("event: error") is None
    assert (
        parser.feed(
            'data: {"detail": "Bedrock request failed", "dependency": "bedrock", '
            '"http_equivalent": 502}'
        )
        is None
    )
    event = parser.feed("")

    assert event is not None
    assert event.event == "error"
    assert event.data["dependency"] == "bedrock"
    assert event.data["http_equivalent"] == 502
    # The parser resets cleanly after a dispatched frame.
    assert parser.feed("") is None
