"""Token chunker (tiktoken cl100k) with two strategies.

Pure and cheap — no network, AWS, or embedding calls. Besides the ingest
pipeline, this is reused by the dedup completeness count (expected chunk
count for a doc), so it must be deterministic for identical input.

Strategies (selected via ``INGESTION_CHUNK_STRATEGY``, default "recursive"):

- ``recursive``: LangChain's ``RecursiveCharacterTextSplitter`` configured
  via ``from_tiktoken_encoder`` so chunk size and overlap are measured in
  cl100k tokens: the text is recursively split at natural separators
  (paragraphs, lines, sentences, words) and the pieces merged into chunks of
  at most ``chunk_tokens`` tokens. Overlap is built from whole pieces, so
  consecutive chunks share up to ``chunk_overlap`` tokens of trailing
  context (a target, not an exact count).
- ``fixed``: LangChain's ``TokenTextSplitter`` — hard cl100k token windows
  of exactly ``chunk_tokens`` tokens (last chunk shorter) with exactly
  ``chunk_overlap`` tokens of overlap. Ignores separators and may cut
  mid-sentence; exists for chunking-strategy experiments.
"""

from functools import lru_cache

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter, TokenTextSplitter

from app.config import get_settings

_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


@lru_cache(maxsize=8)
def _splitter(
    strategy: str, chunk_tokens: int, chunk_overlap: int
) -> RecursiveCharacterTextSplitter | TokenTextSplitter:
    if strategy == "fixed":
        return TokenTextSplitter(
            encoding_name="cl100k_base",
            chunk_size=chunk_tokens,
            chunk_overlap=chunk_overlap,
        )
    if strategy == "recursive":
        return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=chunk_tokens,
            chunk_overlap=chunk_overlap,
            separators=_SEPARATORS,
        )
    raise ValueError(f"Unknown chunk_strategy: {strategy!r} (use 'recursive' or 'fixed')")


def chunk_text(
    text: str,
    chunk_tokens: int | None = None,
    chunk_overlap: int | None = None,
    strategy: str | None = None,
) -> list[str]:
    """Split `text` into chunks of at most `chunk_tokens` tokens with up to
    `chunk_overlap` tokens of overlap between consecutive chunks. Sizes and
    strategy default from settings."""
    settings = get_settings()
    if chunk_tokens is None:
        chunk_tokens = settings.chunk_tokens
    if chunk_overlap is None:
        chunk_overlap = settings.chunk_overlap
    if strategy is None:
        strategy = settings.chunk_strategy
    if chunk_overlap >= chunk_tokens:
        raise ValueError("chunk_overlap must be smaller than chunk_tokens")

    if not text.strip():
        return []
    return _splitter(strategy, chunk_tokens, chunk_overlap).split_text(text)
