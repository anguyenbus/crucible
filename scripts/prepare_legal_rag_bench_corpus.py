"""
Prepare Legal RAG Bench corpus for ChromaDB ingestion.

Downloads the corpus split from HuggingFace and exports to text files
for the stub ChromaDB RAG implementation.
"""

from __future__ import annotations

import os
from pathlib import Path


def prepare_corpus(
    output_dir: Path = Path("data/rag/legal_rag_bench/corpus_files"),
    force_refresh: bool = False,
) -> None:
    """
    Download and export Legal RAG Bench corpus to text files.

    Args:
        output_dir: Directory to write corpus text files.
        force_refresh: If True, re-download even if files exist.

    """
    # Get HF token
    token = os.environ.get("HF_TOKEN")
    if not token:
        token_path = Path.home() / ".huggingface" / "token"
        if token_path.exists():
            token = token_path.read_text().strip()

    # Import here to avoid dependency if not used
    try:
        from datasets import load_dataset
    except ImportError as err:
        raise ImportError(
            "datasets library not installed. Install with: uv add datasets"
        ) from err

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load corpus split
    print("Loading Legal RAG Bench corpus from HuggingFace...")
    corpus = load_dataset(
        "isaacus/legal-rag-bench",
        name="corpus",
        split="test",
        token=token,
    )

    print(f"Corpus size: {len(corpus)} passages")

    # Export to text files
    for item in corpus:
        passage_id = item.get("id", "")
        title = item.get("title", "")
        text = item.get("text", "")
        footnotes = item.get("footnotes", "")

        # Skip if no id or text
        if not passage_id or not text:
            continue

        # Build filename (sanitize passage_id for filesystem)
        safe_id = passage_id.replace("/", "_").replace("\\", "_")
        output_path = output_dir / f"{safe_id}.txt"

        # Skip if exists and not forcing refresh
        if output_path.exists() and not force_refresh:
            continue

        # Build document content
        content = f"ID: {passage_id}\n"
        if title:
            content += f"Title: {title}\n"
        content += f"\n{text}\n"
        if footnotes:
            content += f"\nFootnotes:\n{footnotes}\n"

        # Write file
        output_path.write_text(content, encoding="utf-8")

    print(f"Corpus exported to: {output_dir}")
    print(f"Files created: {len(list(output_dir.glob('*.txt')))}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare Legal RAG Bench corpus for ChromaDB"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/rag/legal_rag_bench/corpus_files"),
        help="Output directory for corpus text files",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-download even if files exist",
    )

    args = parser.parse_args()

    prepare_corpus(output_dir=args.output_dir, force_refresh=args.force_refresh)
