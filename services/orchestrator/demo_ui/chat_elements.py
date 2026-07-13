"""
Chainlit view helpers that need ``chainlit`` at hand.

Kept OUT of the deliberately chainlit-free ``chat_ui/`` package AND out of
``app.py`` (which cannot be imported in a unit test — ``chainlit run`` loads it
by file path, and its name would collide with the orchestrator's ``app`` package
the demo_ui firewall forbids). This module is a thin, browser-free seam: pure
over the render model plus ``cl.Text`` construction, no network, no orchestrator
import — so the element wiring is unit-assertable without a running frontend.

MANUAL guarantees (NOT automatable here — Task Group 3): that Chainlit's frontend
actually renders each ``[n]`` in the message content as a clickable source chip
and that clicking it opens the side pane with this chunk. This module can only
assert the STRUCTURE the frontend needs (bracketed names that appear in the sent
content, ``display="side"``); the live render/click is the manual acceptance drill.
"""

from __future__ import annotations

import chainlit as cl

from chat_ui import render


def numbered_source_elements(numbered: render.NumberedAnswer) -> list[cl.Text]:
    """
    One clickable ``cl.Text`` side element per cited chunk, keyed by its ``[n]``.

    ``name`` is ``source.anchor`` VERBATIM — the bracketed ``"[n]"`` token that
    also appears in ``numbered.text``. Chainlit's frontend (2.11.1) matches
    element names against the message content with a regex-escaped substring
    alternation and NO word boundaries, so the ``name`` MUST be the full bracketed
    token; a bare digit would turn every digit in the answer into a link. The
    caller attaches these to the ANSWER message — elements are ``for_id``-scoped,
    so a chip is clickable only in the message it rides on.

    Honesty: the pane shows the RETRIEVED CHUNK the model cited — evidence for a
    human to judge, NOT proof of correctness and NOT "the source document". Zero
    cited chunks → no elements.
    """
    return [
        cl.Text(
            name=source.anchor,
            content=(
                f"**Retrieved chunk {source.anchor}** — rank {source.rank}, "
                f"score {source.score:.4f}, chunk_id `{source.chunk_id}`\n\n"
                f"{source.text}"
            ),
            display="side",
        )
        for source in numbered.sources
    ]
