"""
Request/response models for ``POST /analyze`` — the non-RAG, single-document
analysis path (facts extraction + summary).

Unlike ``/query`` this endpoint does NO retrieval: the caller SUPPLIES the full
document text (the BFF forwards a document's indexed chunk text), and the
generator runs once over it. It therefore never touches OpenSearch and works
even when the retrieval client is unavailable. Config resolution is reused only
to pin the SAME generator model as ``/query`` (``pipeline_config`` → generator
model id); sampling is fixed low for reproducible structured extraction.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.config import DEFAULT_PIPELINE_CONFIG_REF

# Which analysis to run. The UI drives these independently ("Extract" → facts,
# "Summarise" → summary) so each is its own generation; "both" runs one prompt
# that returns both (kept for callers that want a single round-trip).
AnalyzeMode = Literal["facts", "summary", "both"]


class AnalyzeRequest(BaseModel):
    """One document's text to analyse into a summary and/or discrete facts."""

    text: str = Field(
        min_length=1,
        description="The full document text to analyse (e.g. joined chunk text).",
    )
    mode: AnalyzeMode = Field(
        default="both",
        description="Which analysis to run: 'facts', 'summary', or 'both'.",
    )
    pipeline_config: str = Field(
        default=DEFAULT_PIPELINE_CONFIG_REF,
        description=(
            "Fully-qualified config ref pinning the generator model, e.g. "
            "'legal-rag-default-1.8.0'. Only the generator model id is used."
        ),
    )
    max_facts: int = Field(
        default=12,
        ge=1,
        le=50,
        description="Upper bound on the number of extracted facts returned.",
    )


class AnalyzeResponse(BaseModel):
    """The extracted summary + facts, plus provenance for the caller."""

    summary: str = Field(description="A concise plain-English summary of the document.")
    facts: list[str] = Field(
        description="Discrete factual statements explicitly supported by the document."
    )
    model_id: str = Field(description="The Bedrock generator model id that produced this.")
    truncated: bool = Field(
        description="True when the input text exceeded the cap and was truncated before analysis."
    )
