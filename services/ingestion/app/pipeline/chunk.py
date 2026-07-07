"""Recursive token chunker (LangChain RecursiveCharacterTextSplitter, tiktoken cl100k).

Pure and cheap — no network, AWS, or embedding calls. Besides the ingest
pipeline, this is reused by the dedup completeness count (expected chunk
count for a doc), so it must be deterministic for identical input.

Splitting is delegated to LangChain's ``RecursiveCharacterTextSplitter``
configured via ``from_tiktoken_encoder`` so chunk size and overlap are
measured in cl100k tokens: the text is recursively split at natural
separators (paragraphs, lines, sentences, words) and the pieces merged into
chunks of at most ``chunk_tokens`` tokens. Overlap is built from whole
pieces, so consecutive chunks share up to ``chunk_overlap`` tokens of
trailing context (a target, not an exact count).
"""

from functools import lru_cache

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import get_settings

_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


@lru_cache(maxsize=8)
def _splitter(chunk_tokens: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base",
        chunk_size=chunk_tokens,
        chunk_overlap=chunk_overlap,
        separators=_SEPARATORS,
    )


def chunk_text(
    text: str,
    chunk_tokens: int | None = None,
    chunk_overlap: int | None = None,
) -> list[str]:
    """Split `text` into chunks of at most `chunk_tokens` tokens with up to
    `chunk_overlap` tokens of overlap between consecutive chunks. Sizes
    default from settings."""
    settings = get_settings()
    if chunk_tokens is None:
        chunk_tokens = settings.chunk_tokens
    if chunk_overlap is None:
        chunk_overlap = settings.chunk_overlap
    if chunk_overlap >= chunk_tokens:
        raise ValueError("chunk_overlap must be smaller than chunk_tokens")

    if not text.strip():
        return []
    return _splitter(chunk_tokens, chunk_overlap).split_text(text)
