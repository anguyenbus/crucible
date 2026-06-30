"""Tests for GST Legal RAG corpus preparation script."""

from pathlib import Path

from dev.scripts.prepare_gst_legal_rag_corpus import (
    _build_corpus_content,
    _sanitize_filename,
    prepare_corpus,
)


def test_sanitize_filename():
    """Test filename sanitisation."""
    # Test forward slash replacement
    assert _sanitize_filename("doc/id/123") == "doc_id_123"
    # Test backslash replacement
    assert _sanitize_filename("doc\\id\\456") == "doc_id_456"
    # Test mixed separators
    assert _sanitize_filename("doc/id\\456") == "doc_id_456"
    # Test no separators needed
    assert _sanitize_filename("doc_123") == "doc_123"


def test_build_corpus_content_with_title():
    """Test corpus content building with title."""
    content = _build_corpus_content(
        passage_id="GII_GSTIIFL1_NAT_ATO_00001_sec499_c1_s1",
        title="Decision",
        text="This is the text content.",
        footnotes=None,
    )

    expected = (
        "ID: GII_GSTIIFL1_NAT_ATO_00001_sec499_c1_s1\n"
        "Title: Decision\n"
        "\n"
        "This is the text content.\n"
    )
    assert content == expected


def test_build_corpus_content_with_footnotes():
    """Test corpus content building with footnotes."""
    content = _build_corpus_content(
        passage_id="doc_123",
        title="Section Title",
        text="Main text here.",
        footnotes="Footnote content here.",
    )

    expected = (
        "ID: doc_123\n"
        "Title: Section Title\n"
        "\n"
        "Main text here.\n"
        "\n"
        "Footnotes:\nFootnote content here.\n"
    )
    assert content == expected


def test_build_corpus_content_minimal():
    """Test corpus content building with minimal fields."""
    content = _build_corpus_content(
        passage_id="minimal_doc",
        title="",
        text="Text only.",
        footnotes=None,
    )

    # When title is empty, only ID and text
    expected = "ID: minimal_doc\n\nText only.\n"
    assert content == expected


def test_prepare_corpus_creates_files(tmp_path: Path):
    """Test corpus preparation creates correct number of files."""
    # Create mock documents.jsonl
    documents_file = tmp_path / "documents.jsonl"
    documents_file.write_text(
        '{"id": "doc_1", "title": "Title 1", "text": "Text 1", "footnotes": null}\n'
        '{"id": "doc/2", "title": "Title 2", "text": "Text 2", "footnotes": null}\n'
        '{"id": "doc_3", "title": "Title 3", "text": "Text 3", "footnotes": null}\n'
    )

    output_dir = tmp_path / "corpus_files"
    prepare_corpus(cache_dir=tmp_path, output_dir=output_dir)

    # Verify files created
    txt_files = list(output_dir.glob("*.txt"))
    assert len(txt_files) == 3

    # Verify specific files exist
    assert (output_dir / "doc_1.txt").exists()
    assert (output_dir / "doc_2.txt").exists()  # sanitized from doc/2
    assert (output_dir / "doc_3.txt").exists()


def test_prepare_corpus_file_content(tmp_path: Path):
    """Test corpus preparation file content format."""
    documents_file = tmp_path / "documents.jsonl"
    documents_file.write_text(
        '{"id": "test_doc", "title": "Test Title", '
        '"text": "Test text content.", "footnotes": null}\n'
    )

    output_dir = tmp_path / "corpus_files"
    prepare_corpus(cache_dir=tmp_path, output_dir=output_dir)

    # Read and verify content
    content = (output_dir / "test_doc.txt").read_text(encoding="utf-8")

    expected = "ID: test_doc\nTitle: Test Title\n\nTest text content.\n"
    assert content == expected


def test_prepare_corpus_skips_existing_without_force(tmp_path: Path):
    """Test that existing files are skipped without force_refresh."""
    documents_file = tmp_path / "documents.jsonl"
    documents_file.write_text(
        '{"id": "doc_1", "title": "Title", "text": "Original text", "footnotes": null}\n'
    )

    output_dir = tmp_path / "corpus_files"
    output_dir.mkdir(parents=True)

    # Create existing file with different content
    existing_file = output_dir / "doc_1.txt"
    existing_file.write_text("Existing content\n")

    # Run without force_refresh
    prepare_corpus(cache_dir=tmp_path, output_dir=output_dir, force_refresh=False)

    # File should still have original content
    content = existing_file.read_text(encoding="utf-8")
    assert content == "Existing content\n"


def test_prepare_corpus_overwrites_with_force(tmp_path: Path):
    """Test that existing files are overwritten with force_refresh."""
    documents_file = tmp_path / "documents.jsonl"
    documents_file.write_text(
        '{"id": "doc_1", "title": "Title", "text": "New text", "footnotes": null}\n'
    )

    output_dir = tmp_path / "corpus_files"
    output_dir.mkdir(parents=True)

    # Create existing file with different content
    existing_file = output_dir / "doc_1.txt"
    existing_file.write_text("Old content\n")

    # Run with force_refresh
    prepare_corpus(cache_dir=tmp_path, output_dir=output_dir, force_refresh=True)

    # File should have new content
    content = existing_file.read_text(encoding="utf-8")
    assert "New text" in content
    assert "Old content" not in content
