"""
Request/response models for ``POST /compare`` — the non-RAG, two-document
CONTRADICTION-detection path.

Like ``/analyze`` this endpoint does NO retrieval: the caller SUPPLIES both
documents' full text (the BFF forwards each document's indexed chunk text) and
the model runs over them, so it never touches OpenSearch and works even when
retrieval is down. Config resolution is reused ONLY to pin the SAME generator
model as ``/query`` (``pipeline_config`` -> generator model id); sampling is
fixed low for reproducible structured extraction.

The comparison follows the CLAIRE (EMNLP 2025) *decompose-then-verify* shape,
adapted for two specific documents rather than a corpus: each document is first
decomposed into atomic claims (each carrying its exact supporting quote), then
the two claim sets are cross-examined for genuine contradictions. Every
contradiction is classified into one of six types and carries the exact
conflicting quote from EACH document, so the caller can render the source text
verbatim (never paraphrased).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.config import DEFAULT_PIPELINE_CONFIG_REF

# The contradiction taxonomy the UI renders. Kept as an explicit, closed set so
# the model classifies into a stable vocabulary (mirrors CLAIRE's typed
# discrepancies; validated against its NumericalDiscrepancy/TemporalDiscrepancy/
# ... superset). Keep in sync with the BFF + frontend labels.
ContradictionType = Literal[
    "temporal",
    "numerical",
    "authority",
    "process",
    "policy_reversal",
    "specificity",
]


class Contradiction(BaseModel):
    """One genuine contradiction between the two documents.

    ``quote_a``/``quote_b`` are the EXACT conflicting spans from document A and
    document B respectively — verbatim source text, never a paraphrase, so the
    reader can see the conflict as each document states it.
    """

    type: ContradictionType = Field(
        description="Which of the six contradiction categories this conflict is.",
    )
    description: str = Field(
        description="One-sentence, plain-English statement of what actually conflicts.",
    )
    quote_a: str = Field(
        description="The exact conflicting text as stated in document A.",
    )
    quote_b: str = Field(
        description="The exact conflicting text as stated in document B.",
    )


class ContradictionReport(BaseModel):
    """Structured-output container for the cross-examination stage."""

    contradictions: list[Contradiction] = Field(
        default_factory=list,
        description="Every genuine contradiction found; empty when the documents agree.",
    )


class CompareRequest(BaseModel):
    """The two documents' text to cross-examine for contradictions."""

    document_a: str = Field(
        min_length=1,
        description="Full text of the first document (e.g. joined chunk text).",
    )
    document_b: str = Field(
        min_length=1,
        description="Full text of the second document (e.g. joined chunk text).",
    )
    label_a: str = Field(
        default="Document A",
        description="Human label for document A (e.g. its filename) used in prompts + output.",
    )
    label_b: str = Field(
        default="Document B",
        description="Human label for document B (e.g. its filename) used in prompts + output.",
    )
    pipeline_config: str = Field(
        default=DEFAULT_PIPELINE_CONFIG_REF,
        description=(
            "Fully-qualified config ref pinning the generator model, e.g. "
            "'legal-rag-default-1.8.0'. Only the generator model id + region are used."
        ),
    )


class CompareResponse(BaseModel):
    """The classified contradictions plus provenance for the caller."""

    contradictions: list[Contradiction] = Field(
        description="Genuine contradictions between the two documents (empty when they agree)."
    )
    model_id: str = Field(description="The Bedrock generator model id that produced this.")
    truncated: bool = Field(
        description="True when either document exceeded the cap and was truncated before analysis."
    )
