"""HTTP client for the orchestrator's non-RAG ``POST /analyze`` endpoint.

The BFF consumes the orchestrator over its env-var URL ONLY
(``WEBUI_ORCHESTRATOR_URL``) — it never imports the orchestrator package and
never holds a Bedrock client itself (dependency firewall). ``/analyze`` runs
the generator ONCE over supplied document text and returns a structured
``{summary, facts, model_id, truncated}``; a non-2xx response or network fault
is normalized to a typed :class:`AnalyzeServiceError` carrying the
HTTP-equivalent code so the caller surfaces an honest status rather than an
opaque hang or 500.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# Generation over a whole document can take a while; give it room past the
# chat-token timeout without hanging a request forever.
ANALYZE_TIMEOUT_SECONDS = 120.0


class AnalyzeServiceError(Exception):
    """A typed analysis failure carrying the HTTP-equivalent code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class AnalyzeResult:
    summary: str
    facts: list[str]
    model_id: str
    truncated: bool


def analyze_text(
    text: str, orchestrator_url: str, *, mode: str = "both", max_facts: int = 12
) -> AnalyzeResult:
    """Analyse ``text`` into a summary and/or facts via the orchestrator.

    ``mode`` is ``facts`` / ``summary`` / ``both``. A non-2xx or network fault
    becomes :class:`AnalyzeServiceError` so the caller can map it to an honest
    status instead of a swallowed 500.
    """
    url = f"{orchestrator_url.rstrip('/')}/analyze"
    try:
        response = httpx.post(
            url,
            json={"text": text, "mode": mode, "max_facts": max_facts},
            timeout=ANALYZE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise AnalyzeServiceError(502, f"orchestrator unreachable at {url}: {exc}") from exc
    if response.status_code == 200:
        body = response.json()
        return AnalyzeResult(
            summary=str(body.get("summary", "")),
            facts=[str(f) for f in body.get("facts", [])],
            model_id=str(body.get("model_id", "")),
            truncated=bool(body.get("truncated", False)),
        )
    raise AnalyzeServiceError(response.status_code, _detail(response))


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
