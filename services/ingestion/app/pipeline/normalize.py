"""Markdown normalization: collapse whitespace while keeping code fences intact.

Pure local stage. The normalized text is what gets hashed (SHA-256 dedup),
chunked, and indexed — so this must be deterministic for identical input.
"""

import re

# Fenced code blocks (``` or ~~~), including an unterminated trailing fence.
_FENCED_BLOCK_RE = re.compile(r"(```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z))", re.DOTALL)

_HORIZONTAL_WHITESPACE_RE = re.compile(r"[ \t]+")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def normalize(text: str) -> str:
    """Collapse whitespace in prose segments; leave fenced code blocks untouched."""
    parts = _FENCED_BLOCK_RE.split(text)
    normalized = [
        part if part.startswith(("```", "~~~")) else _collapse_whitespace(part)
        for part in parts
    ]
    return "".join(normalized).strip()


def _collapse_whitespace(segment: str) -> str:
    lines = [
        _HORIZONTAL_WHITESPACE_RE.sub(" ", line).strip()
        for line in segment.split("\n")
    ]
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", "\n".join(lines))
