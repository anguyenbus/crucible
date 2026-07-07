"""Task Group 4: deterministic doc_id and chunk _id helpers."""

import hashlib
import re

from app.pipeline.hash_dedup import chunk_id, derive_doc_id


def test_doc_id_is_truncated_sha256_of_source_uri():
    uri = "s3://atlas-demo-shared-s3-docs/guides/setup.md"

    doc_id = derive_doc_id(uri)

    assert doc_id == hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]
    assert re.fullmatch(r"[0-9a-f]{16}", doc_id)
    assert derive_doc_id(uri) == doc_id  # deterministic
    # Documented POC caveat: local path vs S3 URI of the same file differ.
    assert derive_doc_id("/home/admin/guides/setup.md") != doc_id


def test_chunk_id_format_is_doc_id_colon_index():
    assert chunk_id("abc123def4567890", 0) == "abc123def4567890:0"
    assert chunk_id("abc123def4567890", 17) == "abc123def4567890:17"
