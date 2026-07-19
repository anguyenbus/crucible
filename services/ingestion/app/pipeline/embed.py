"""Embedding stage: Titan Embed Text v2 via Bedrock invoke_model.

Used for both chunk embedding (ingest) and query embedding (search). Calls
are STRICTLY SEQUENTIAL — no concurrency, no batching (POC decision); the
only throttling defense is exponential backoff with full jitter.

Titan v2 input limits (characterized live by scripts/check_titan.py):
  - 8192-token input limit — a hard ValidationException, not truncation.
  - 50,000-character request cap on inputText, which ordinary prose hits
    (~5,900 tokens) BEFORE the token limit.
Both are guarded client-side. Token counts use tiktoken cl100k — an
approximation of Titan's tokenizer, acceptable because ~800-token chunks sit
far below either limit; a rare near-limit miss still fails safely as an
`EmbeddingUpstreamError`.
"""

import json
import random
import time

from botocore.exceptions import ClientError

from app.clients.bedrock import get_bedrock_client
from app.config import get_settings
from app.pipeline.chunk import count_tokens

TITAN_TOKEN_LIMIT = 8192
TITAN_CHAR_LIMIT = 50_000

MAX_ATTEMPTS = 6
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 8.0

_THROTTLING_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
    "LimitExceededException",
}


class EmbeddingInputTooLargeError(ValueError):
    """Input text exceeds Titan's token or character limit."""


class EmbeddingUpstreamError(RuntimeError):
    """Bedrock embedding call failed (maps to a 502 at the API layer)."""


def embed_text(text: str, client=None) -> list[float]:
    """Embed one text into a 1024-dim vector, retrying throttling with
    exponential backoff + full jitter."""
    _guard_input_limits(text)
    if client is None:
        client = get_bedrock_client()
    settings = get_settings()
    body = json.dumps({"inputText": text, "dimensions": settings.embed_dimensions})

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.invoke_model(
                modelId=settings.embed_model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in _THROTTLING_CODES or attempt == MAX_ATTEMPTS - 1:
                raise EmbeddingUpstreamError(
                    f"Bedrock embedding call failed ({code or 'unknown error'})."
                ) from exc
            delay = min(BASE_DELAY_SECONDS * (2**attempt), MAX_DELAY_SECONDS)
            time.sleep(delay + random.uniform(0, delay))
            continue

        vector = json.loads(response["body"].read()).get("embedding")
        if not isinstance(vector, list) or len(vector) != settings.embed_dimensions:
            raise EmbeddingUpstreamError(
                "Bedrock returned an unexpected embedding payload."
            )
        return vector

    raise EmbeddingUpstreamError("Bedrock embedding retries exhausted.")


def embed_texts_iter(texts: list[str], client=None):
    """Yield one 1024-dim vector per text, in order — strictly sequential.

    The generator form lets a caller report per-chunk progress ("chunk i of N")
    as each embedding completes; a raised `EmbeddingUpstreamError` propagates
    from the offending chunk exactly as in `embed_texts`.
    """
    if client is None:
        client = get_bedrock_client()
    for text in texts:
        yield embed_text(text, client=client)


def embed_texts(texts: list[str], client=None) -> list[list[float]]:
    """Embed chunks one at a time, in order — strictly sequential by design."""
    return list(embed_texts_iter(texts, client=client))


def _guard_input_limits(text: str) -> None:
    # Character cap first: it is the cheaper check and, for prose, the
    # binding constraint (hit before the token limit).
    if len(text) > TITAN_CHAR_LIMIT:
        raise EmbeddingInputTooLargeError(
            f"Embedding input is {len(text)} characters; Titan v2 caps "
            f"inputText at {TITAN_CHAR_LIMIT} characters."
        )
    tokens = count_tokens(text)
    if tokens > TITAN_TOKEN_LIMIT:
        raise EmbeddingInputTooLargeError(
            f"Embedding input is ~{tokens} tokens; Titan v2 caps input at "
            f"{TITAN_TOKEN_LIMIT} tokens."
        )
