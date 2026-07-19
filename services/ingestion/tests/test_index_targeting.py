"""Task Group 3: additive per-request `index` targeting on write + prune.

Focused tests only (per task 3.1): the REQUIRED absent-`index` regression
(write + prune both target `settings.index_name`) and the present-`index`
case (write + prune target that index only). The OpenSearch client is mocked.
Exhaustive index-name-validation permutations live with the provision
endpoint (Group 5) and are skipped here.
"""

from app.config import get_settings
from app.pipeline.index import index_chunks, prune_stale_chunks


class FakeBulkClient:
    """Records the `_index` on each bulk index action."""

    def __init__(self):
        self.bulk_bodies: list[list[dict]] = []

    def bulk(self, body, refresh=None):
        self.bulk_bodies.append(body)
        return {"errors": False, "items": []}

    def indexed_indices(self) -> set[str]:
        """The set of `_index` targets across all recorded action lines."""
        indices: set[str] = set()
        for body in self.bulk_bodies:
            for line in body:
                if "index" in line and "_index" in line["index"]:
                    indices.add(line["index"]["_index"])
        return indices


class FakeDeleteClient:
    def __init__(self):
        self.delete_calls: list[dict] = []

    def delete_by_query(self, index=None, body=None, **kwargs):
        self.delete_calls.append({"index": index, "body": body, **kwargs})
        return {"deleted": 0}


CHUNKS = ["chunk one", "chunk two"]
VECTORS = [[0.1] * 1024, [0.2] * 1024]


def test_absent_index_writes_and_prunes_the_default_index():
    """REQUIRED regression: no `index` ⇒ both target `settings.index_name`."""
    default_index = get_settings().index_name  # "genai-ingestion-md"

    bulk = FakeBulkClient()
    index_chunks(
        CHUNKS, VECTORS, doc_id="doc1", source_uri="s3://b/k.md", sha256="s", client=bulk
    )
    assert bulk.indexed_indices() == {default_index}

    delete = FakeDeleteClient()
    prune_stale_chunks("doc1", keep_sha256="s", client=delete)
    assert delete.delete_calls[0]["index"] == default_index


def test_present_index_writes_and_prunes_that_index_only():
    """A per-project `index` scopes both the write and the prune."""
    project_index = "proj-abc123"

    bulk = FakeBulkClient()
    index_chunks(
        CHUNKS,
        VECTORS,
        doc_id="doc1",
        source_uri="s3://b/k.md",
        sha256="s",
        index=project_index,
        client=bulk,
    )
    assert bulk.indexed_indices() == {project_index}

    delete = FakeDeleteClient()
    prune_stale_chunks("doc1", keep_sha256="s", index=project_index, client=delete)
    assert delete.delete_calls[0]["index"] == project_index
