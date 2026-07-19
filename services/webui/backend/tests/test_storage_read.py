"""`storage.read_stored`: local-path + s3:// branches, boto3 MOCKED.

The s3 branch is exercised without live AWS by stubbing `boto3.client`; a
missing key/file raises the typed `StoredObjectMissingError` (→ API 404), while
a genuine infra fault raises `StorageError` (→ 5xx).
"""

import io

import pytest
from botocore.exceptions import ClientError

from app import storage


def test_read_local_pdf_returns_bytes_and_pdf_content_type(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4 data")

    data, content_type = storage.read_stored(str(path))

    assert data == b"%PDF-1.4 data"
    assert content_type == "application/pdf"


def test_read_local_markdown_content_type(tmp_path):
    path = tmp_path / "note.markdown"
    path.write_bytes(b"# hi")
    _, content_type = storage.read_stored(str(path))
    assert content_type == "text/markdown"


def test_read_local_missing_file_raises_typed_missing(tmp_path):
    with pytest.raises(storage.StoredObjectMissingError):
        storage.read_stored(str(tmp_path / "absent.pdf"))


def test_read_s3_returns_bytes_and_content_type(monkeypatch):
    class _FakeS3:
        def get_object(self, Bucket, Key):
            assert Bucket == "my-bucket"
            assert Key == "projects/p1/report.pdf"
            return {"Body": io.BytesIO(b"%PDF s3 bytes")}

    monkeypatch.setattr(storage.boto3, "client", lambda *a, **k: _FakeS3())

    data, content_type = storage.read_stored("s3://my-bucket/projects/p1/report.pdf")

    assert data == b"%PDF s3 bytes"
    assert content_type == "application/pdf"


def test_read_s3_missing_key_raises_typed_missing(monkeypatch):
    class _FakeS3:
        def get_object(self, Bucket, Key):
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "nope"}}, "GetObject"
            )

    monkeypatch.setattr(storage.boto3, "client", lambda *a, **k: _FakeS3())

    with pytest.raises(storage.StoredObjectMissingError):
        storage.read_stored("s3://my-bucket/gone.pdf")
