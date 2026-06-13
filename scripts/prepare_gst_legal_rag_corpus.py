"""
Prepare GST Legal RAG corpus for ChromaDB ingestion.

Reads the vendored GST documents.jsonl and exports to text files
for the stub ChromaDB RAG implementation.
"""

from __future__ import annotations

import json
from pathlib import Path


def _sanitize_filename(passage_id: str) -> str:
    """
    Sanitize passage ID for filesystem safety.

    Replaces forward slashes and backslashes with underscores.

    Args:
        passage_id: Raw passage ID from dataset.

    Returns:
        Sanitized filename-safe string.

    """
    return passage_id.replace("/", "_").replace("\\", "_")


def _build_corpus_content(
    passage_id: str,
    title: str,
    text: str,
    footnotes: str | None,
) -> str:
    """
    Build corpus file content from document fields.

    Format follows Legal RAG Bench corpus structure:
    - ID line (always)
    - Title line (when present)
    - Empty line
    - Text content (always)
    - Empty line
    - Footnotes section (when present)

    Args:
        passage_id: Document passage ID.
        title: Document title (may be empty).
        text: Main document text.
        footnotes: Footnotes content (may be None).

    Returns:
        Formatted corpus content string.

    """
    content = f"ID: {passage_id}\n"

    if title:
        content += f"Title: {title}\n"

    content += f"\n{text}\n"

    if footnotes:
        content += f"\nFootnotes:\n{footnotes}\n"

    return content


def prepare_corpus(
    cache_dir: Path = Path("data/rag/gst_legal_rag"),
    output_dir: Path = Path("data/rag/gst_legal_rag/corpus_files"),
    force_refresh: bool = False,
) -> None:
    """
    Export GST Legal RAG corpus to text files.

    Reads documents.jsonl from the cache directory and exports each document
    as a text file in the corpus output directory.

    Args:
        cache_dir: Directory containing documents.jsonl.
        output_dir: Directory to write corpus text files.
        force_refresh: If True, overwrite existing files.

    Raises:
        FileNotFoundError: If documents.jsonl does not exist in cache_dir.

    """
    # Path to documents.jsonl
    documents_path = cache_dir / "documents.jsonl"

    if not documents_path.exists():
        raise FileNotFoundError(
            f"documents.jsonl not found in {cache_dir}. "
            f"Please ensure the GST dataset files are vendored correctly."
        )

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Read and export documents
    with documents_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                doc = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in documents.jsonl: {e}") from e

            passage_id = doc.get("id", "")
            title = doc.get("title", "")
            text = doc.get("text", "")
            footnotes = doc.get("footnotes")

            # Skip if no id or text
            if not passage_id or not text:
                continue

            # Build filename (sanitize passage_id for filesystem)
            safe_id = _sanitize_filename(passage_id)
            output_path = output_dir / f"{safe_id}.txt"

            # Skip if exists and not forcing refresh
            if output_path.exists() and not force_refresh:
                continue

            # Build document content
            content = _build_corpus_content(passage_id, title, text, footnotes)

            # Write file
            output_path.write_text(content, encoding="utf-8")

    print(f"Corpus exported to: {output_dir}")
    print(f"Files created: {len(list(output_dir.glob('*.txt')))}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prepare GST Legal RAG corpus for ChromaDB")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("data/rag/gst_legal_rag"),
        help="Directory containing documents.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/rag/gst_legal_rag/corpus_files"),
        help="Output directory for corpus text files",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Overwrite existing files",
    )

    args = parser.parse_args()

    prepare_corpus(
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
        force_refresh=args.force_refresh,
    )
