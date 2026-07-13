"""Shared fixtures: mock AWS clients + the canned payload (now a TEST fixture).

Phase 2 deleted the canned path from the router; the canned chunks/answer
live HERE so mocked-client tests get a deterministic, schema-valid end-to-end
response without any AWS access. The mocks are installed via the app.state
substitution seam (lifespan only constructs clients when none are pre-set),
so no live client construction ever happens under pytest.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.clients import AppClients
from app.clients.bedrock import GenerationResult
from app.config import Settings
from app.main import app as main_app
from app.routers import health as health_module
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Canned payload (moved from the Phase 1 router). Contract-realistic id
# semantics: chunk_id is "{doc_id}:{chunk_idx}" per BYO index contract v2.
# ---------------------------------------------------------------------------

CANNED_DOC_ID = "gst-act-1999"

CANNED_CHUNKS: list[dict[str, Any]] = [
    {
        "chunk_id": f"{CANNED_DOC_ID}:0",
        "rank": 0,
        "score": 0.91,
        "doc_id": CANNED_DOC_ID,
        "text": (
            "Section 38-190 provides that a supply of services made to a "
            "non-resident who is not in Australia when the thing supplied is "
            "done is GST-free."
        ),
    },
    {
        "chunk_id": f"{CANNED_DOC_ID}:1",
        "rank": 1,
        "score": 0.78,
        "doc_id": CANNED_DOC_ID,
        "text": (
            "Division 38 sets out the supplies that are GST-free; if a supply "
            "is GST-free, no GST is payable on the supply."
        ),
    },
]

# The canned "generated" answer keeps markers IN the text (eval convention).
# It cites both retrieved chunks and one hallucinated id, so the citation
# builder's drop-but-count path is exercised end-to-end (dropped count = 1).
CANNED_ANSWER_TEXT = (
    f"Based on the retrieved provisions, the supply is GST-free under "
    f"section 38-190 [{CANNED_DOC_ID}:0]. Division 38 confirms that no GST "
    f"is payable on a GST-free supply [{CANNED_DOC_ID}:1]. An unfounded "
    f"aside cites [made-up-doc:9]."
)

CANNED_GENERATION = GenerationResult(
    text=CANNED_ANSWER_TEXT,
    model_id="au.anthropic.claude-sonnet-4-6",
    input_tokens=321,
    output_tokens=42,
    stop_reason="end_turn",
)

# Fixture location facts: a fake endpoint so the Q8 provenance echo
# (system_version.opensearch_host) is deterministic in tests.
FIXTURE_SETTINGS = Settings(opensearch_endpoint="https://search-legal.example.com")


def canned_stream_deltas() -> list[str]:
    """CANNED_ANSWER_TEXT split into deltas; concatenation is exact."""
    return [CANNED_ANSWER_TEXT[:60], CANNED_ANSWER_TEXT[60:140], CANNED_ANSWER_TEXT[140:]]


class MockGenerationStream:
    """GenerationStream stand-in: scripted deltas + optional mid-stream error."""

    def __init__(
        self,
        deltas: list[str],
        generation: GenerationResult,
        *,
        error: Exception | None = None,
        error_after_deltas: int = 1,
    ) -> None:
        self._deltas = deltas
        self._generation = generation
        self._error = error
        self._error_after = error_after_deltas
        self._finished = False

    def __iter__(self):
        for position, delta in enumerate(self._deltas):
            if self._error is not None and position == self._error_after:
                raise self._error
            yield delta
        if self._error is not None:
            raise self._error
        self._finished = True

    def result(self) -> GenerationResult:
        if not self._finished:
            raise RuntimeError("MockGenerationStream.result() before full consumption.")
        return self._generation


def canned_hits(text_field: str = "content") -> list[dict[str, Any]]:
    """OpenSearch-shaped hits that map back to CANNED_CHUNKS verbatim."""
    return [
        {
            "_id": chunk["chunk_id"],
            "_score": chunk["score"],
            "_source": {"doc_id": chunk["doc_id"], text_field: chunk["text"]},
        }
        for chunk in CANNED_CHUNKS
    ]


class MockBedrockClient:
    """BedrockClient stand-in: canned embedding/generation, recorded calls."""

    def __init__(self) -> None:
        self.embed_calls: list[dict[str, Any]] = []
        self.generate_calls: list[dict[str, Any]] = []
        self.generate_stream_calls: list[dict[str, Any]] = []
        self.credentials_calls = 0
        # Failure injection: raise these from embed_query()/generate() when set.
        self.embed_error: Exception | None = None
        self.generate_error: Exception | None = None
        # Payload injection: generate() returns this instead of CANNED_GENERATION.
        self.generation_result: GenerationResult | None = None
        # Streaming injection: scripted deltas + optional mid-stream error
        # raised after `stream_error_after_deltas` deltas have been yielded.
        self.stream_deltas: list[str] | None = None
        self.stream_error: Exception | None = None
        self.stream_error_after_deltas: int = 1
        self.credentials_ok = True

    def embed_query(self, text: str, *, model_id: str, **kwargs: Any) -> list[float]:
        self.embed_calls.append({"text": text, "model_id": model_id})
        if self.embed_error is not None:
            raise self.embed_error
        return [0.03125] * 1024

    def generate(
        self, prompt: str, *, model_id: str, temperature: float, max_tokens: int
    ) -> GenerationResult:
        self.generate_calls.append(
            {
                "prompt": prompt,
                "model_id": model_id,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.generate_error is not None:
            raise self.generate_error
        return self.generation_result if self.generation_result is not None else CANNED_GENERATION

    def generate_stream(
        self, prompt: str, *, model_id: str, temperature: float, max_tokens: int
    ) -> MockGenerationStream:
        self.generate_stream_calls.append(
            {
                "prompt": prompt,
                "model_id": model_id,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.generate_error is not None:
            # Initial-call failure (e.g. throttle exhaustion before any delta).
            raise self.generate_error
        deltas = self.stream_deltas if self.stream_deltas is not None else canned_stream_deltas()
        generation = (
            self.generation_result if self.generation_result is not None else CANNED_GENERATION
        )
        return MockGenerationStream(
            deltas,
            generation,
            error=self.stream_error,
            error_after_deltas=self.stream_error_after_deltas,
        )

    def credentials_resolve(self) -> bool:
        self.credentials_calls += 1
        return self.credentials_ok


class MockSearchClient:
    """OpenSearchSearchClient stand-in: canned hits, recorded calls."""

    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.index_exists_calls = 0
        # Failure injection: raise these when set.
        self.search_error: Exception | None = None
        # Payload injection: search() returns these hits instead of canned_hits().
        self.search_hits: list[dict[str, Any]] | None = None
        self.index_exists_result = True

    def search(self, body: dict[str, Any], *, search_pipeline: str) -> dict[str, Any]:
        self.search_calls.append({"body": body, "search_pipeline": search_pipeline})
        if self.search_error is not None:
            raise self.search_error
        hits = self.search_hits if self.search_hits is not None else canned_hits()
        return {"hits": {"hits": hits}}

    def index_exists(self) -> bool:
        self.index_exists_calls += 1
        return self.index_exists_result


@pytest.fixture
def mock_bedrock() -> MockBedrockClient:
    return MockBedrockClient()


@pytest.fixture
def mock_search() -> MockSearchClient:
    return MockSearchClient()


@pytest.fixture
def client(mock_bedrock: MockBedrockClient, mock_search: MockSearchClient) -> TestClient:
    """TestClient with mock clients installed via the app.state seam."""
    main_app.state.settings = FIXTURE_SETTINGS
    main_app.state.clients = AppClients(bedrock=mock_bedrock, search=mock_search)
    try:
        yield TestClient(main_app)
    finally:
        del main_app.state.settings
        del main_app.state.clients


@pytest.fixture(autouse=True)
def _fresh_readyz_cache():
    """The readyz TTL cache must never leak between tests."""
    reset = getattr(health_module, "_reset_readyz_cache", lambda: None)
    reset()
    yield
    reset()
