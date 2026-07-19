"""
``POST /query`` / ``POST /query/stream`` request contract (Pydantic v2).

The request carries the current ``question`` plus an OPTIONAL bounded
``history`` (Phase B multi-turn memory). The original "single-turn only —
session fields fail loudly" note is superseded by this DELIBERATE extension
(spec: ``agent-os/specs/2026-07-13-chainlit-chat-ui``); ``extra="forbid"``
still rejects every other unknown field.

Contract impact (roadmap item 23, decided): ``history`` is REQUEST-ONLY and
is NEVER echoed anywhere in ``result`` — ``result.query.text`` continues to
echo the CURRENT ``question`` verbatim and ``metadata`` stays opaque — so
eval's ``rag_query_output`` v1.1.0 contract and every consumer (eval's
``orchestrator_query.py``) are UNCHANGED; absent ``history`` ≡ single-turn
behavior exactly. The rewritten retrieval query is observable in the
retrieval span's ``INPUT_VALUE`` — the honest provenance channel for "what
was actually retrieved against".

There is still NO per-request ``top_k`` override — ``top_k`` comes only from
the pinned pipeline config; a different ``top_k`` means publishing a NEW
``{name}-{semver}`` config version. This was dropped by decision, not
deferred — it MUST NOT be added.

``retrieval_indices`` (project-scoped chat) is the ONE other deliberate
request field: an OPTIONAL index SCOPE (a LOCATION fact, never a behavior
pin), additive and byte-identical-when-absent — see its field description.
"""

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# Fully-qualified pinned-config reference: {name}-{semver}, where the trailing
# component is a strict X.Y.Z semver (e.g. "legal-rag-default-1.0.0"). No
# floating refs, no `latest` alias. Plain (numbered) capture groups — group(1)
# is the name, group(2) the semver — because this pattern is exported into
# OpenAPI, and Python-only `(?P<...>)` named groups are not valid ECMA-262.
PIPELINE_CONFIG_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"^([a-z0-9]+(?:[-_][a-z0-9]+)*)-(\d+\.\d+\.\d+)$"
)

# Hard request bounds for the multi-turn history (schema-enforced with 422).
# Clients (e.g. the Chainlit demo UI) truncate to the SAME bounds; the
# config-pinned rewrite/prompt windows narrow further, never widen.
MAX_HISTORY_TURNS: Final[int] = 20
MAX_HISTORY_TURN_CHARS: Final[int] = 8_000


class HistoryTurn(BaseModel):
    """
    One prior conversation turn, oldest-first in ``QueryRequest.history``.

    REQUEST-ONLY input to the deterministic query rewrite and the prompt
    history block — never echoed in ``result``. Unknown keys are rejected
    (``extra="forbid"``) so contract drift fails loudly.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = Field(
        description="Who spoke the turn: the user or the assistant."
    )
    text: str = Field(
        max_length=MAX_HISTORY_TURN_CHARS,
        description="The turn's text (max 8,000 chars; longer turns are rejected with 422).",
    )


class QueryRequest(BaseModel):
    """
    RAG query request: the current question plus optional bounded history.

    Unknown/extra fields are rejected (``extra="forbid"``) so contract drift —
    e.g. a ``top_k`` override — fails loudly at validation. ``history`` is the
    ONE deliberate session field (Phase B), superseding the earlier
    "session fields fail loudly" note; it is request-only and never echoed.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(description="The user question to answer (the current turn).")
    history: list[HistoryTurn] | None = Field(
        default=None,
        max_length=MAX_HISTORY_TURNS,
        description=(
            "Optional prior conversation turns, oldest first (max 20 turns, "
            "8,000 chars per turn). Consumed ONLY by the deterministic "
            "history-aware query rewrite and the prompt history block (windows "
            "and char budgets are pinned in the pipeline config). REQUEST-ONLY: "
            "never echoed anywhere in result — result.query.text echoes the "
            "current question verbatim, so rag_query_output v1.1.0 is unchanged "
            "and absent history behaves exactly like a single-turn request. The "
            "rewritten retrieval query is observable in the retrieval span's "
            "INPUT_VALUE."
        ),
    )
    query_id: str | None = Field(
        default=None,
        description=(
            "Optional stable identifier for the query; echoed back in "
            "result.query.query_id. Eval joins on this field to ground truth."
        ),
    )
    pipeline_config: str = Field(
        pattern=PIPELINE_CONFIG_REF_RE.pattern,
        description=(
            "REQUIRED fully-qualified pinned-config reference in {name}-{semver} "
            "form, e.g. 'legal-rag-default-1.0.0'. Exact match only: no floating "
            "refs, no 'latest' alias, no default-to-newest. Unknown-but-well-formed "
            "refs return 404; refs not matching this shape return 422."
        ),
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional opaque object echoed back VERBATIM in result.query.metadata "
            "and NEVER read by any pipeline stage — pipeline behavior is "
            "metadata-invariant. Enables eval dataset joins for replay "
            "(parallels eval's query.metadata)."
        ),
    )
    retrieval_indices: list[str] | None = Field(
        default=None,
        description=(
            "Optional per-request retrieval index SCOPE: the OpenSearch index (or "
            "indices) to search. A single list mapping 1:1 to OpenSearch's native "
            "comma-separated multi-index — the orchestrator stays index-agnostic "
            "(no 'which one is general' logic, no per-project config). ABSENT "
            "(the eval / demo_ui default) is byte-identical to today: the single "
            "Settings opensearch_index (legal-rag-bench) is queried and echoed. "
            "This is a LOCATION FACT, never a behavior pin — top_k, config "
            "resolution, and config_sha256 are unaffected. PRESENT → exactly the "
            "given indices are queried (comma-joined for OpenSearch) and "
            "system_version.opensearch_index echoes that scope so replay stays "
            "attributable."
        ),
    )
