"""Upload storage: S3 via the boto3 credential chain, local-volume fallback.

When AWS credentials are present and `WEBUI_UPLOAD_BUCKET` is set, bytes go to
S3 and the canonical `s3://bucket/key` URI is returned. When credential-less
(dev), bytes are written under `WEBUI_UPLOAD_DIR` and the resulting LOCAL
ABSOLUTE path is returned. Either way the exact string returned is what gets
passed to ingestion as its `source` and recorded on the document row.

`read_stored` is the inverse used by the document-detail view: given the exact
`storage_uri` recorded at upload (an `s3://bucket/key` URI OR a local absolute
path), it returns the stored bytes and a content type derived from the filename
extension. A missing object/file raises `StoredObjectMissingError` so the API
surfaces an honest 404, never an opaque 500.

Honest doc_id caveat (from ingestion): the SAME file ingested via a local path
vs its `s3://` URI yields DIFFERENT ingestion doc_ids -- the source string is
hashed. We do not paper over that; the storage_uri we record is the source we
actually used.
"""

from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

# Content types for serving the stored original back to the left pane. Browsers
# render pdf/markdown/image/html inline; Office types (.docx/.xlsx) have no native
# inline viewer, so they fall through to octet-stream and download — the parser
# still extracts their text for the right pane regardless.
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _has_aws_credentials() -> bool:
    try:
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def _object_key(project_id: str, filename: str) -> str:
    return f"projects/{project_id}/{filename}"


def _content_type_for(name: str) -> str:
    return _CONTENT_TYPES.get(Path(name).suffix.lower(), _DEFAULT_CONTENT_TYPE)


def store_upload(project_id: str, filename: str, data: bytes) -> str:
    """Persist bytes and return the source string to hand to ingestion."""
    settings = get_settings()

    if settings.upload_bucket and _has_aws_credentials():
        key = _object_key(project_id, filename)
        client = boto3.client("s3", region_name=settings.aws_region)
        try:
            client.put_object(Bucket=settings.upload_bucket, Key=key, Body=data)
        except (BotoCoreError, ClientError) as exc:  # noqa: TRY003 - infra fault surfaces as 5xx
            raise StorageError(f"failed to store upload to S3: {exc}") from exc
        return f"s3://{settings.upload_bucket}/{key}"

    dest_dir = (Path(settings.upload_dir).resolve()) / "projects" / project_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    dest.write_bytes(data)
    return str(dest)


def read_stored(storage_uri: str) -> tuple[bytes, str]:
    """Read the bytes at `storage_uri`; return (bytes, content_type).

    Handles an `s3://bucket/key` URI (boto3 `get_object`) and a local absolute
    path (read bytes). Content type is derived from the filename extension
    (`.pdf`/`.md`/`.markdown`, else octet-stream). A missing object/file raises
    `StoredObjectMissingError` (→ the API returns 404, not 500).
    """
    if storage_uri.startswith("s3://"):
        parsed = urlparse(storage_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        client = boto3.client("s3", region_name=get_settings().aws_region)
        try:
            obj = client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:  # noqa: TRY003 - missing object → typed 404
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                raise StoredObjectMissingError(
                    f"stored object not found: {storage_uri}"
                ) from exc
            raise StorageError(f"failed to read upload from S3: {exc}") from exc
        except BotoCoreError as exc:  # noqa: TRY003 - infra fault surfaces as 5xx
            raise StorageError(f"failed to read upload from S3: {exc}") from exc
        return obj["Body"].read(), _content_type_for(key)

    path = Path(storage_uri)
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        raise StoredObjectMissingError(
            f"stored file not found: {storage_uri}"
        ) from exc
    return data, _content_type_for(path.name)


def delete_stored(storage_uri: str) -> bool:
    """Best-effort delete of the stored file/object; return True if it acted.

    Used when a document is deleted so we don't orphan the uploaded bytes. This
    is intentionally forgiving: an already-absent object is a no-op (False), and
    a transient fault is swallowed (returns False) rather than blocking the
    delete — the authoritative cleanup is the index + row removal, not the blob.
    """
    try:
        if storage_uri.startswith("s3://"):
            parsed = urlparse(storage_uri)
            client = boto3.client("s3", region_name=get_settings().aws_region)
            client.delete_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
            return True
        Path(storage_uri).unlink(missing_ok=True)
        return True
    except (BotoCoreError, ClientError, OSError):
        return False


class StorageError(RuntimeError):
    """A genuine storage-infrastructure fault (surfaces to the client as 5xx)."""


class StoredObjectMissingError(RuntimeError):
    """The stored object/file is absent (surfaces to the client as 404)."""
