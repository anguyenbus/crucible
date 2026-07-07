"""Task Group 4: recursive token chunker (boundaries, overlap, determinism).

The chunker delegates to LangChain's RecursiveCharacterTextSplitter
(from_tiktoken_encoder), so overlap is built from whole split pieces: up to
~120 tokens shared between consecutive chunks, not an exact 120-token tail.
Unique numbered sentences keep the text non-periodic so the measured
suffix/prefix overlap is the true overlap.
"""

from app.pipeline.chunk import chunk_text, count_tokens

# ~6500 tokens of unique short-sentence prose (~13 tokens per sentence).
LONG_TEXT = " ".join(
    f"This is uniquely numbered sentence {i} in the long test document."
    for i in range(500)
)


def _suffix_prefix_overlap(previous: str, current: str) -> str:
    """The longest suffix of `previous` that is a prefix of `current`."""
    best = 0
    for n in range(1, min(len(previous), len(current)) + 1):
        if previous.endswith(current[:n]):
            best = n
    return current[:best]


def test_short_document_yields_single_chunk():
    text = "Just a short markdown document."

    assert chunk_text(text) == [text]


def test_chunk_boundaries_near_800_tokens():
    chunks = chunk_text(LONG_TEXT)

    assert len(chunks) > 1
    for chunk in chunks:
        assert count_tokens(chunk) <= 800
    for chunk in chunks[:-1]:  # all but the final remainder chunk fill the budget
        assert count_tokens(chunk) >= 800 - 60


def test_consecutive_chunks_overlap_by_about_120_tokens():
    chunks = chunk_text(LONG_TEXT)

    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:]):
        overlap = _suffix_prefix_overlap(previous, current)
        # Overlap is whole pieces totalling at most 120 tokens.
        assert 90 <= count_tokens(overlap) <= 120


def test_chunking_is_deterministic_for_identical_input():
    assert chunk_text(LONG_TEXT) == chunk_text(LONG_TEXT)
