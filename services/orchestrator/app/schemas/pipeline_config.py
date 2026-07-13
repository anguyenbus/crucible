"""
FULL pinned pipeline-config schema (Pydantic v2).

The complete config shape is defined NOW even though some fields are unused
until Phase 3 — this avoids config-format churn across phases. Config
artifacts (``app/configs/{name}-{semver}.yaml``) carry BEHAVIOR pins only
(model/prompt/retrieval/budgets); endpoints live in env-driven Settings
(``app.config``) — the two never blur.

All models are frozen and ``extra="forbid"``: a released config is immutable
and unknown YAML keys fail loudly instead of being silently ignored.

Phase 2 evolution: the prompt template TEXT is inlined in the config YAML (as
a block scalar) so ``config_sha256`` covers every prompt byte — no packaged
prompt files, no second hash manifest. Fields added in Phase 2 (template
text, context character budget, generation ``max_tokens``) carry schema
DEFAULTS so the released ``legal-rag-default-1.0.0`` stays RESOLVABLE without
ever being edited; the defaults exist for backward RESOLUTION only — new
configs must pin every value explicitly.

Phase B evolution (multi-turn memory): the ``history`` pins block (rewrite/
prompt windows and char budgets) follows the same pattern — schema defaults
keep 1.0.0/1.1.0 resolvable; ``legal-rag-default-1.2.0`` pins the block
explicitly alongside its history-aware prompt template.

Phase 3 evolution (system-prompt-leakage input guard): the ``GuardrailsPin``
gains ``enabled``/``classifier_model_id``/``input_categories`` — again with
backward-RESOLUTION defaults so the pre-guard configs stay resolvable and are
never edited; ``legal-rag-default-1.3.0`` pins them explicitly to enable the
guard.
"""

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

_PIN_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

# Backward-RESOLUTION defaults (never for authoring new configs; see module
# docstring). max_tokens mirrors eval's generator parity value; the character
# budget comfortably fits top_k=8 legal-text chunks without a tokenizer.
DEFAULT_GENERATION_MAX_TOKENS: Final[int] = 1024
DEFAULT_CONTEXT_CHAR_BUDGET: Final[int] = 32_000
# History pins (Phase B multi-turn memory): backward-RESOLUTION defaults so
# the released 1.0.0/1.1.0 configs stay resolvable without ever being edited.
# Rewrite window: recent USER turns prefixed to the retrieval query; prompt
# window: full recent turns rendered into the {history} prompt block.
DEFAULT_HISTORY_REWRITE_WINDOW_USER_TURNS: Final[int] = 3
DEFAULT_HISTORY_REWRITE_CHAR_BUDGET: Final[int] = 1_500
DEFAULT_HISTORY_PROMPT_WINDOW_TURNS: Final[int] = 6
DEFAULT_HISTORY_PROMPT_CHAR_BUDGET: Final[int] = 6_000
DEFAULT_PROMPT_TEMPLATE_TEXT: Final[str] = (
    "You are a helpful assistant that answers questions based on the provided "
    "context.\n"
    "Each context block starts with its chunk id in square brackets. When "
    "answering, you MUST cite your sources by repeating the chunk id verbatim "
    "in square brackets, e.g. [gst-act-1999:4].\n"
    "If the context doesn't contain enough information to answer the question "
    "confidently, say \"I don't have enough information to answer this "
    'question."\n'
    "\n"
    "Context:\n"
    "{context}\n"
    "\n"
    "Question: {question}\n"
    "\n"
    "Answer:"
)
# Guard input categories (Phase 3): backward-RESOLUTION default so pre-guard
# configs stay resolvable; only 'prompt_leak' is active in this slice.
DEFAULT_GUARDRAIL_INPUT_CATEGORIES: Final[tuple[str, ...]] = ("prompt_leak",)


class GeneratorPin(BaseModel):
    """Generation model pin (Bedrock-only constraint shapes the values)."""

    model_config = _PIN_MODEL_CONFIG

    model_id: str = Field(
        description="Bedrock generator model/inference-profile id (au.* profile).",
    )
    temperature: float = Field(
        description="Sampling temperature pinned for reproducible generation.",
    )
    max_tokens: int = Field(
        default=DEFAULT_GENERATION_MAX_TOKENS,
        gt=0,
        description=(
            "Maximum generated tokens per answer. Default exists only so the "
            "released 1.0.0 config stays resolvable — new configs pin this "
            "explicitly."
        ),
    )


class EmbedderPin(BaseModel):
    """Embedding model pin."""

    model_config = _PIN_MODEL_CONFIG

    model_id: str = Field(description="Bedrock embedding model id.")


class PromptTemplatePin(BaseModel):
    """
    Prompt-template pin with the template TEXT inlined.

    The template text lives INSIDE the config YAML (block scalar) so
    ``config_sha256`` covers every prompt byte — "identical config ⇒ identical
    behavior" is literally true. ``version`` remains for human labeling; the
    Phase 1 ``ref`` indirection is gone (template sharing across configs is a
    deliberate later decision).
    """

    model_config = _PIN_MODEL_CONFIG

    version: str = Field(description="Prompt-template version (human labeling).")
    text: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE_TEXT,
        description=(
            "Full prompt template text with {context} and {question} "
            "placeholders (replace-rendered, so literal braces elsewhere in "
            "the template survive). Default exists only so the released "
            "1.0.0 config stays resolvable — new configs inline the text "
            "explicitly."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_ref(cls, data: Any) -> Any:
        """
        Ignore the Phase 1 ``ref`` key for backward RESOLUTION of 1.0.0.

        The released ``legal-rag-default-1.0.0.yaml`` is immutable and carries
        ``prompt_template.ref``; the field is retired in Phase 2. This shim
        drops ONLY that legacy key — every other unknown key still fails
        loudly via ``extra="forbid"``.
        """
        if isinstance(data, dict) and "ref" in data:
            data = {key: value for key, value in data.items() if key != "ref"}
        return data


class RetrievalPins(BaseModel):
    """
    Retrieval behavior pins.

    ``top_k`` lives ONLY here — there is no per-request override; a different
    ``top_k`` means publishing a new ``{name}-{semver}`` config version.
    """

    model_config = _PIN_MODEL_CONFIG

    top_k: int = Field(gt=0, description="Number of chunks the retriever returns.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Engine-specific retrieval parameters (e.g. search type) pinned "
            "alongside top_k. Index/pipeline/field NAMES are location facts "
            "and live in env-driven Settings, never here."
        ),
    )


class ContextPins(BaseModel):
    """Context-assembly behavior pins (consumed by the context assembler)."""

    model_config = _PIN_MODEL_CONFIG

    char_budget: int = Field(
        default=DEFAULT_CONTEXT_CHAR_BUDGET,
        gt=0,
        description=(
            "CHARACTER budget for the assembled context (no tokenizer "
            "dependency); whole-chunk tail truncation only. Default exists "
            "only so the released 1.0.0 config stays resolvable — new "
            "configs pin this explicitly."
        ),
    )


class HistoryPins(BaseModel):
    """
    Multi-turn history behavior pins (Phase B).

    Consumed by the deterministic history-aware query rewrite and the prompt
    builder's ``{history}`` block — both stay pure functions of these pins.
    Field defaults exist ONLY so released pre-history configs (1.0.0/1.1.0)
    keep resolving — new configs pin the whole block explicitly.
    """

    model_config = _PIN_MODEL_CONFIG

    rewrite_window_user_turns: int = Field(
        default=DEFAULT_HISTORY_REWRITE_WINDOW_USER_TURNS,
        ge=0,
        description=(
            "How many recent USER turns (selected newest-first, rendered "
            "oldest-first) are prefixed to the retrieval query."
        ),
    )
    rewrite_char_budget: int = Field(
        default=DEFAULT_HISTORY_REWRITE_CHAR_BUDGET,
        gt=0,
        description=(
            "CHARACTER budget for the rewrite prefix (whole-turn truncation, "
            "oldest dropped first; no tokenizer dependency)."
        ),
    )
    prompt_window_turns: int = Field(
        default=DEFAULT_HISTORY_PROMPT_WINDOW_TURNS,
        ge=0,
        description=(
            "How many recent turns (user AND assistant) render into the "
            "prompt's {history} block, chronological order."
        ),
    )
    prompt_char_budget: int = Field(
        default=DEFAULT_HISTORY_PROMPT_CHAR_BUDGET,
        gt=0,
        description=(
            "CHARACTER budget for the rendered history lines (whole-turn "
            "tail truncation, oldest dropped first)."
        ),
    )


class GuardrailsPin(BaseModel):
    """
    Guardrail policy pin (consumed from Phase 3).

    Phase 3 evolution (system-prompt-leakage input guard): the ``enabled``/
    ``classifier_model_id``/``input_categories`` fields carry backward-
    RESOLUTION defaults so the released pre-guard configs (1.0.0/1.1.0/1.2.0)
    stay resolvable with the guard OFF and are NEVER edited — the defaults
    exist for backward RESOLUTION only, exactly like the Phase 2 template/
    context and Phase B history pins. A ``policy_version`` string alone must
    NOT switch behavior: ``enabled`` (plus a pinned classifier id) is the
    actual gate.
    """

    model_config = _PIN_MODEL_CONFIG

    policy_version: str = Field(
        description="Guardrail policy version applied to input/output stages.",
    )
    enabled: bool = Field(
        default=False,
        description=(
            "Master gate. False ⇒ check_input is a typed IDENTITY and NO "
            "classifier client is consulted (the eval/1.0.0/1.1.0/1.2.0 path). "
            "Default exists only so the released pre-guard configs stay "
            "resolvable — new guard-enabling configs pin this explicitly."
        ),
    )
    classifier_model_id: str | None = Field(
        default=None,
        description=(
            "Bedrock guard-classifier model/inference-profile id (au.* Haiku). "
            "REQUIRED when enabled=True (validated at resolution); None when "
            "the guard is off."
        ),
    )
    input_categories: tuple[str, ...] = Field(
        default=DEFAULT_GUARDRAIL_INPUT_CATEGORIES,
        description=(
            "Active input-guard detector classes. Only 'prompt_leak' in this "
            "slice; the tuple is EXTENSIBLE. Default exists only for backward "
            "resolution of released configs."
        ),
    )

    @model_validator(mode="after")
    def _require_classifier_when_enabled(self) -> "GuardrailsPin":
        """enabled=True demands a pinned classifier id — fail loudly otherwise."""
        if self.enabled and not self.classifier_model_id:
            raise ValueError(
                "guardrails.enabled is true but classifier_model_id is unset — "
                "an enabled input guard MUST pin a Bedrock classifier model id "
                "(the guard cannot be enabled by a policy_version string alone)."
            )
        return self


class PipelineConfig(BaseModel):
    """
    One released, immutable pipeline config (``{name}-{semver}``).

    Loaded from a packaged YAML artifact in ``app/configs/`` by
    ``app.config.resolve_pipeline_config``; ``version`` feeds
    ``result.system_version.pipeline_version``.
    """

    model_config = _PIN_MODEL_CONFIG

    name: str = Field(description="Config family name (e.g. 'legal-rag-default').")
    version: str = Field(description="Config semver; echoed as pipeline_version.")
    region: str = Field(
        description="AWS region pinned for Bedrock model ids (data residency).",
    )
    generator: GeneratorPin
    embedder: EmbedderPin
    prompt_template: PromptTemplatePin
    retrieval: RetrievalPins
    context: ContextPins = Field(
        default_factory=ContextPins,
        description=(
            "Context-assembly pins. Defaulted only so the released 1.0.0 "
            "config stays resolvable — new configs pin the block explicitly."
        ),
    )
    history: HistoryPins = Field(
        default_factory=HistoryPins,
        description=(
            "Multi-turn history pins. Defaulted only so the released "
            "pre-history configs (1.0.0/1.1.0) stay resolvable — new configs "
            "pin the block explicitly."
        ),
    )
    guardrails: GuardrailsPin
