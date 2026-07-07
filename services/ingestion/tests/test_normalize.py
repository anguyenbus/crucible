"""Task Group 4: markdown normalization (whitespace collapsing, code fences kept)."""

from app.pipeline.normalize import normalize


def test_normalize_collapses_whitespace():
    text = "# Title\n\n\n\n\nSome    text\twith   extra   spaces   \nnext line\n"

    assert normalize(text) == "# Title\n\nSome text with extra spaces\nnext line"


def test_normalize_preserves_code_fences():
    fence = "```python\ndef  f():\n    return   1\n```"
    text = f"Intro   text\n\n\n\n{fence}\n\nOutro    text"

    result = normalize(text)

    assert fence in result  # code fence content untouched, indentation intact
    assert "Intro text" in result
    assert "Outro text" in result
