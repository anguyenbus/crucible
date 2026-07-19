"""HTTP client for the orchestrator's non-RAG ``POST /compare`` endpoint.

The BFF consumes the orchestrator over its env-var URL ONLY
(``WEBUI_ORCHESTRATOR_URL``) — it never imports the orchestrator package and
never holds a Bedrock/LangChain client itself (dependency firewall). ``/compare``
runs the decompose-then-verify pipeline over both documents' supplied text and
returns a typed contradiction report; a non-2xx response or network fault is
normalized to a typed :class:`CompareServiceError` carrying the HTTP-equivalent
code so the caller surfaces an honest status rather than an opaque hang or 500.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# Comparison decomposes BOTH documents and then cross-examines them (multiple
# Bedrock calls), so give it more room than the single-shot /analyze without
# hanging a request forever.
COMPARE_TIMEOUT_SECONDS = 180.0


class CompareServiceError(Exception):
    """A typed comparison failure carrying the HTTP-equivalent code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class Contradiction:
    type: str
    description: str
    quote_a: str
    quote_b: str


@dataclass
class CompareResult:
    contradictions: list[Contradiction]
    model_id: str
    truncated: bool


def compare_documents(
    text_a: str,
    text_b: str,
    orchestrator_url: str,
    *,
    label_a: str = "Document A",
    label_b: str = "Document B",
) -> CompareResult:
    """Cross-examine two documents for contradictions via the orchestrator.

    A non-2xx or network fault becomes :class:`CompareServiceError` so the
    caller can map it to an honest status instead of a swallowed 500.
    """
    url = f"{orchestrator_url.rstrip('/')}/compare"
    try:
        response = httpx.post(
            url,
            json={
                "document_a": text_a,
                "document_b": text_b,
                "label_a": label_a,
                "label_b": label_b,
            },
            timeout=COMPARE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise CompareServiceError(502, f"orchestrator unreachable at {url}: {exc}") from exc
    if response.status_code == 200:
        body = response.json()
        return CompareResult(
            contradictions=[
                Contradiction(
                    type=str(c.get("type", "")),
                    description=str(c.get("description", "")),
                    quote_a=str(c.get("quote_a", "")),
                    quote_b=str(c.get("quote_b", "")),
                )
                for c in body.get("contradictions", [])
            ],
            model_id=str(body.get("model_id", "")),
            truncated=bool(body.get("truncated", False)),
        )
    raise CompareServiceError(response.status_code, _detail(response))


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"orchestrator returned HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if isinstance(detail, str):
        return detail
    return str(body)
