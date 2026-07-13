"""
Regression: on_message must aclose the SSE generator IN the event loop on break.

on_message breaks out of ``async for event in stream_query(...)`` on the terminal
``final``/``error`` event. ``stream_query`` is an async generator suspended inside
httpx's ``async with ...stream()``; a bare break only SUSPENDS it, so Python later
closes it during GC — outside the loop — where its httpx teardown cannot await,
raising ``RuntimeError: async generator ignored GeneratorExit`` (the observed
error, made vivid by a guard block that sends one ``final`` then closes).

Wrapping it in ``contextlib.aclosing`` makes the break ``aclose()`` the generator
deterministically, in-loop. This test guards that idiom with a generator shaped
like ``stream_query`` (an ``async with`` whose teardown must ``await``).
"""

import asyncio
from contextlib import aclosing


async def _fake_stream(events, teardown):
    """Mimic stream_query: yield events from inside an await-on-exit context."""

    class _AwaitingResource:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            await asyncio.sleep(0)  # teardown that MUST run in the loop (like httpx aclose)
            teardown["closed"] = True

    async with _AwaitingResource():
        for event in events:
            yield event


async def _consume_like_on_message(events, teardown):
    """The on_message pattern: aclosing + async-for + break on the terminal event."""
    async with aclosing(_fake_stream(events, teardown)) as stream:
        async for event in stream:
            if event == "final":
                break
    # Captured IN the loop, immediately after the aclosing block: the generator's
    # `async with` teardown has already run — not deferred to GC/shutdown.
    return teardown["closed"]


def test_break_on_final_closes_the_stream_generator_in_loop():
    teardown = {"closed": False}
    closed_at_block_exit = asyncio.run(
        _consume_like_on_message(["token", "token", "final", "never-reached"], teardown)
    )
    assert closed_at_block_exit is True, (
        "breaking on `final` must aclose the SSE generator in the event loop — "
        "otherwise it is closed later during GC and raises "
        "'async generator ignored GeneratorExit'"
    )
