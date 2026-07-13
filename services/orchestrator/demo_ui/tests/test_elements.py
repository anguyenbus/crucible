"""
Structural tests for the clickable-citation elements (browser-free, remaining T4).

These assert the STRUCTURE Chainlit's frontend needs to make a ``[n]`` marker a
clickable source chip: every element ``name`` is the bracketed ``[n]`` anchor
VERBATIM and appears in the answer message content, one element per cited chunk,
``display="side"``, and a header carrying rank/score/chunk_id plus the retrieved
chunk text. Importing ``chainlit`` is fine (a demo_ui dependency); a running
frontend is not needed — ``init_http_context()`` inside a short-lived event loop
(the suite's ``asyncio.run`` convention) gives ``cl.Text`` the session context it
reads at construction, WITHOUT a browser or server. The returned elements hold
plain values, so their attributes are asserted after the loop closes.

MANUAL (NOT automatable here — Task Group 3): that Chainlit's frontend actually
RENDERS each ``[n]`` as a clickable chip and OPENS the side pane with the mapped
chunk on click. This suite proves the inputs are correct; the live render/click
is the manual acceptance drill.
"""

import asyncio

from chainlit.context import init_http_context

from chat_elements import numbered_source_elements
from chat_ui.render import NumberedAnswer, NumberedSource


def _build(numbered: NumberedAnswer):
    """Build the elements inside a Chainlit HTTP context (needs a running loop)."""

    async def _run():
        init_http_context()
        return numbered_source_elements(numbered)

    return asyncio.run(_run())


def _numbered() -> NumberedAnswer:
    """A two-source NumberedAnswer whose [n] anchors appear in the text."""
    return NumberedAnswer(
        text="Alpha claim [1]. Beta claim [2].",
        sources=[
            NumberedSource(
                number=1, anchor="[1]", chunk_id="c:1", rank=1, score=0.91, text="chunk one"
            ),
            NumberedSource(
                number=2, anchor="[2]", chunk_id="c:2", rank=2, score=0.72, text="chunk two"
            ),
        ],
    )


def test_element_names_are_bracketed_anchors_present_in_the_sent_content():
    numbered = _numbered()
    elements = _build(numbered)

    # One element per cited chunk, each a side element.
    assert len(elements) == len(numbered.sources)
    for element, source in zip(elements, numbered.sources):
        # name is the anchor VERBATIM — bracketed [n], never a bare digit
        # (a bare digit would make Chainlit's substring matcher link every digit).
        assert element.name == source.anchor
        assert element.name.startswith("[") and element.name.endswith("]")
        # The Chainlit-wiring guarantee deferred from T4: the element name
        # appears in the SENT message content, so the frontend can match it.
        assert element.name in numbered.text
        assert element.display == "side"


def test_element_content_labels_a_retrieved_chunk_with_rank_score_chunk_id():
    element = _build(_numbered())[0]

    # Header carries rank, score, and chunk_id, plus the retrieved chunk text.
    assert "rank 1" in element.content
    assert "0.91" in element.content  # score, formatted
    assert "c:1" in element.content  # chunk_id
    assert "chunk one" in element.content  # the retrieved chunk body
    # Honesty: labelled a retrieved chunk, NEVER "the source document".
    assert "retrieved chunk" in element.content.lower()
    assert "source document" not in element.content.lower()


def test_zero_citation_numbered_answer_yields_no_elements():
    # Empty sources → no cl.Text built at all (no context even needed).
    numbered = NumberedAnswer(text="An answer with no citations at all.", sources=[])
    assert _build(numbered) == []
