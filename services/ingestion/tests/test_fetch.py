"""Task Group 5: fetch stage — source routing and error types (no live AWS)."""

import io

import pytest
from botocore.exceptions import ClientError

from app.pipeline import fetch
from app.pipeline.fetch import InvalidSourceError, SourceNotFoundError, fetch_markdown

MARKDOWN = "# Title\n\nSome body text.\n"


class FakeS3:
    def __init__(self, error_code: str | None = None):
        self.error_code = error_code
        self.calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict:
        self.calls.append((Bucket, Key))
        if self.error_code:
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "not found"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(MARKDOWN.encode("utf-8"))}


def test_s3_uri_routes_to_s3_client(monkeypatch):
    fake_s3 = FakeS3()
    monkeypatch.setattr(fetch, "_s3_client", lambda: fake_s3)

    text = fetch_markdown("s3://atlas-demo-shared-s3-docs/guides/setup.md")

    assert text == MARKDOWN
    assert fake_s3.calls == [("atlas-demo-shared-s3-docs", "guides/setup.md")]


def test_local_absolute_path_reads_file(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text(MARKDOWN, encoding="utf-8")

    assert fetch_markdown(str(path)) == MARKDOWN


def test_missing_s3_object_and_local_file_raise_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch, "_s3_client", lambda: FakeS3(error_code="NoSuchKey"))

    with pytest.raises(SourceNotFoundError):
        fetch_markdown("s3://atlas-demo-shared-s3-docs/missing.md")
    with pytest.raises(SourceNotFoundError):
        fetch_markdown(str(tmp_path / "missing.md"))


def test_unsupported_source_string_raises_invalid_source():
    for source in ("relative/path.md", "https://example.com/doc.md", "s3://bucket"):
        with pytest.raises(InvalidSourceError):
            fetch_markdown(source)
