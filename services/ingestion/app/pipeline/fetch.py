"""Fetch stage: resolve a source string to raw document bytes (or markdown text).

Supported sources:
  - `s3://bucket/key` — the canonical ingestion source.
  - `/abs/path/file` — local absolute path, for testing.

`fetch_bytes` is the bytes path (project-scoped chat, PDF support): it returns
the raw object bytes and the caller's parse stage dispatches by extension
(`.pdf` → pypdf text extraction; `.md`/`.markdown` → UTF-8 decode).
`fetch_markdown` is retained (returns the UTF-8-decoded text) for the
markdown-only callers and the fetch stage tests.

Error types are distinguished so the API layer can map them precisely:
  - `InvalidSourceError` — the source string itself is unsupported (→ 400).
  - `SourceNotFoundError` — the S3 object or local file does not exist (→ 404).
"""

from functools import lru_cache
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings

_S3_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchBucket", "404"}


class InvalidSourceError(ValueError):
    """The source string is not an s3:// URI or a local absolute path."""


class SourceNotFoundError(LookupError):
    """The referenced S3 object or local file does not exist."""


@lru_cache(maxsize=1)
def _s3_client():
    return boto3.client("s3", region_name=get_settings().aws_region)


def fetch_bytes(source: str) -> bytes:
    """Return the raw bytes for `source` (S3 object or local file).

    The bytes path for PDF + markdown: the caller's parse stage decides how to
    turn these bytes into text (UTF-8 decode for markdown, pypdf for PDF).
    """
    if source.startswith("s3://"):
        return _fetch_s3_bytes(source)
    if source.startswith("/"):
        return _fetch_local_bytes(source)
    raise InvalidSourceError(
        f"Unsupported source {source!r}: expected an s3://bucket/key URI "
        "or a local absolute path."
    )


def fetch_markdown(source: str) -> str:
    """Return the raw markdown text for `source` (UTF-8 decoded)."""
    return fetch_bytes(source).decode("utf-8")


def _fetch_s3_bytes(source: str) -> bytes:
    bucket, _, key = source.removeprefix("s3://").partition("/")
    if not bucket or not key:
        raise InvalidSourceError(
            f"Invalid S3 URI {source!r}: expected s3://bucket/key."
        )
    try:
        response = _s3_client().get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _S3_NOT_FOUND_CODES:
            raise SourceNotFoundError(f"S3 object not found: {source}") from exc
        raise
    return response["Body"].read()


def _fetch_local_bytes(source: str) -> bytes:
    path = Path(source)
    if not path.is_file():
        raise SourceNotFoundError(f"Local file not found: {source}")
    return path.read_bytes()
