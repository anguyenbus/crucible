"""
Bedrock Titan Text Embeddings V2 embedder for RAG operations.

NOTE: This is a local-only dev/ component (never imported from app/). It
implements the same embed interface as ``SentenceTransformersEmbedder``
(``embed(texts) -> list[list[float]]``) so the OpenSearch ingest script and
the retriever/CLI can use either embedder interchangeably.

Titan V2 accepts ONE text per InvokeModel call, so ``embed`` loops over the
input with modest thread concurrency and fails fast on the first error.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final

from beartype import beartype

DEFAULT_EMBEDDING_MODEL: Final[str] = "amazon.titan-embed-text-v2:0"
DEFAULT_EMBEDDING_DIM: Final[int] = 1024
DEFAULT_REGION: Final[str] = "ap-southeast-2"
# Modest fan-out: enough to hide per-call latency without hammering Bedrock.
DEFAULT_MAX_WORKERS: Final[int] = 4


@beartype
class BedrockTitanEmbedder:
    """
    Amazon Bedrock Titan Text Embeddings V2 embedder.

    Invokes ``amazon.titan-embed-text-v2:0`` via bedrock-runtime (boto3
    credential chain / instance role) with a fixed output dimension and
    normalization, matching the OpenSearch index contract (1024-dim,
    normalized, cosinesimil).

    Attributes:
        _model_id: Bedrock model identifier.
        _dimensions: Output embedding dimension requested from Titan.
        _normalize: Whether Titan normalizes the returned vector.
        _region: AWS region for the bedrock-runtime client.
        _max_workers: Thread fan-out for multi-text embed calls.
        _client: Lazily created bedrock-runtime client.

    """

    __slots__ = ("_client", "_dimensions", "_max_workers", "_model_id", "_normalize", "_region")

    def __init__(
        self,
        model_id: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_EMBEDDING_DIM,
        normalize: bool = True,
        region_name: str | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        """
        Initialize the embedder.

        Args:
            model_id: Bedrock embedding model ID.
            dimensions: Requested output dimension (must match the index).
            normalize: Request unit-normalized vectors from Titan.
            region_name: AWS region; defaults to ``AWS_REGION`` env, then
                ap-southeast-2.
            max_workers: Max concurrent InvokeModel calls in ``embed``.

        """
        self._model_id: str = model_id
        self._dimensions: int = dimensions
        self._normalize: bool = normalize
        self._region: str = region_name or os.getenv("AWS_REGION", DEFAULT_REGION)
        self._max_workers: int = max_workers
        self._client: Any = None

    def _get_client(self) -> Any:
        """Create the bedrock-runtime client lazily (boto3 credential chain)."""
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def _build_request_body(self, text: str) -> dict:
        """Build the Titan V2 InvokeModel request body for one text."""
        return {
            "inputText": text,
            "dimensions": self._dimensions,
            "normalize": self._normalize,
        }

    def _embed_one(self, text: str) -> list[float]:
        """
        Embed a single text via InvokeModel (Titan takes one text per call).

        Raises:
            ValueError: If the returned vector has an unexpected dimension.

        """
        client = self._get_client()
        response = client.invoke_model(
            modelId=self._model_id,
            body=json.dumps(self._build_request_body(text)),
        )
        payload = json.loads(response["body"].read())
        embedding = payload["embedding"]

        if len(embedding) != self._dimensions:
            raise ValueError(
                f"Titan returned a {len(embedding)}-dim vector; expected {self._dimensions} "
                f"(model {self._model_id})"
            )

        return [float(value) for value in embedding]

    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for texts (order-preserving, fail-fast).

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors (list of floats), one per input text.

        """
        if not texts:
            return []

        if self._max_workers <= 1 or len(texts) == 1:
            return [self._embed_one(text) for text in texts]

        # Initialize the client once before fanning out; boto3 clients are
        # thread-safe for concurrent invoke calls, creation is not.
        self._get_client()
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            # pool.map preserves input order and re-raises the first error.
            return list(pool.map(self._embed_one, texts))


@beartype
def get_bedrock_embedder(
    model_id: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIM,
) -> BedrockTitanEmbedder:
    """
    Get a Bedrock Titan embedder instance.

    Args:
        model_id: Bedrock embedding model ID.
        dimensions: Requested output dimension.

    Returns:
        BedrockTitanEmbedder instance.

    """
    return BedrockTitanEmbedder(model_id=model_id, dimensions=dimensions)
