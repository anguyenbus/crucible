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

NeMo evolution (out-of-process NeMo Guardrails output/facts lane): the
``GuardrailsPin`` gains an OPTIONAL ``nemo`` selector (:class:`NemoGuardPin`)
that gates the pod-backed OUTPUT + FACTS rails. It defaults to ``None`` so the
in-house ``1.0.0``–``1.4.0`` configs resolve with the NeMo lane OFF and are
NEVER edited. PHASE 4 extends :class:`NemoGuardPin` with the two determinism
hashes + image-digest pin and ships the nemo-all ``legal-rag-default-1.8.0.yaml``.
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
# Guard OUTPUT categories (Phase 3, second slice): backward-RESOLUTION default
# so the released pre-output-guard configs (1.0.0/1.1.0/1.2.0/1.3.0) stay
# resolvable with the output guard OFF and are NEVER edited. Empty tuple means
# check_output is a typed identity (no scan). Independent of
# classifier_model_id: the output guard is PURE regex with NO model dependency.
DEFAULT_GUARDRAIL_OUTPUT_CATEGORIES: Final[tuple[str, ...]] = ()


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


class NemoGuardPin(BaseModel):
    """
    Out-of-process NeMo Guardrails **output/facts** selector (NeMo lane wiring).

    Behavior gate ONLY for the NeMo-backed OUTPUT lane (``self check output`` +
    the independently-gated ``self check facts`` grounding rail) served by the
    dedicated ``services/guardrail/`` pod. The pod's base URL is a LOCATION fact
    and lives in env-driven ``Settings`` (``nemo_guard_url``), never here — the
    two never blur, exactly like the OpenSearch endpoint.

    This is the MINIMAL surface the client-wiring slice needs to wire + test the
    output lane. It is optional and defaults to ``None`` on ``GuardrailsPin`` so
    the released ``1.0.0``–``1.4.0`` configs resolve with the NeMo lane OFF and
    are NEVER edited (byte-for-byte).

    Q6b (locked): the INPUT lane keeps the in-house Haiku confirm-step this
    slice — NeMo is layered on OUTPUT + FACTS ONLY — so this pin carries NO
    input toggle. Migrating the input self-check to the pod's
    ``self_check_input`` rail later is a config flip, not new code.

    PHASE 4 EXTENDED THIS MODEL with the provenance/determinism surface that the
    nemo-all ``legal-rag-default-1.8.0.yaml`` pins: ``config_version`` (the
    pod's NeMo config label), ``image_digest`` (the container image the pod runs,
    CI-populated), and the TWO determinism hashes (I4) — ``config_dir_digest`` (a
    digest of the guardrail ``config/`` directory) and ``uv_lock_sha256`` (sha256
    of the guardrail service's resolved ``uv.lock``). Both hashes are computed in
    ``services/guardrail/`` (see ``services/guardrail/app/config_digest.py``) and
    recorded here as LITERAL pinned strings, so — being bytes of the ``1.8.0``
    YAML — they enter the orchestrator's ``config_sha256`` automatically: changing
    the NeMo config OR the dependency lock forces a DIFFERENT ``1.8.0`` pin hash.
    The pod re-computes both live at startup and REFUSES to serve on mismatch, so
    a drifted image cannot answer under a ``1.8.0`` pin. All four fields default to
    ``None`` so the client-wiring-era selectors (and the unit fixtures that build
    ``NemoGuardPin(enabled=True)``) still construct; the shipped ``1.8.0`` config
    pins them explicitly.
    """

    model_config = _PIN_MODEL_CONFIG

    enabled: bool = Field(
        default=False,
        description=(
            "Master gate for the NeMo OUTPUT/facts lane. False ⇒ the "
            "orchestrator never calls the guardrail pod (the 1.0.0–1.4.0 path). "
            "New NeMo-enabling configs pin this explicitly."
        ),
    )
    output_self_check: bool = Field(
        default=True,
        description=(
            "Run the pod's `self check output` rail over the generated answer. "
            "The primary NeMo output rail; on by default when the lane is "
            "enabled."
        ),
    )
    input_self_check: bool = Field(
        default=False,
        description=(
            "Route the INPUT verdict through the pod's `self_check_input` rail "
            "(the nemo-all `1.8.0` config). The orchestrator's regex pre-filter "
            "stays the FREE cost gate (a benign miss makes zero paid pod calls); "
            "on a pre-filter HIT the RAW question is forwarded to the pod, whose "
            "LLM verdict REPLACES the in-house Haiku confirm-step. Default OFF so "
            "the in-house `1.0.0`-`1.4.0` configs keep the input lane on the "
            "in-house classifier (Q6b) and resolve byte-for-byte."
        ),
    )
    check_facts: bool = Field(
        default=False,
        description=(
            "Independently-gated `self check facts` grounding rail. When True "
            "the orchestrator passes check_facts=True to the pod so it runs the "
            "facts rail over answer vs retrieved chunks IN ADDITION to output "
            "self-check; when False the facts rail makes ZERO LLM calls. Default "
            "OFF — facts is the highest-value / highest-FP-risk rail, "
            "A/B-activatable separately from output self-check."
        ),
    )
    config_version: str | None = Field(
        default=None,
        description=(
            "The guardrail pod's NeMo config version label the orchestrator "
            "expects to be answering (human-readable provenance; the byte-exact "
            "identity is config_dir_digest). Default None only so the "
            "client-wiring-era selectors resolve; the nemo-all 1.8.0 config pins it explicitly."
        ),
    )
    image_digest: str | None = Field(
        default=None,
        description=(
            "The guardrail container image digest the pod runs (e.g. "
            "'sha256:...'). CI-POPULATED at build time — a placeholder is "
            "acceptable until an image is built. Default None only for backward "
            "resolution of the client-wiring selectors."
        ),
    )
    config_dir_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Determinism hash (i) (I4): sha256 digest of the guardrail pod's "
            "NeMo config/ directory (config.yml + prompts.yml + any rails/*.co), "
            "computed in services/guardrail/ by app.config_digest. A LITERAL "
            "pinned string so it enters the orchestrator config_sha256; the pod "
            "refuses to serve on live-digest mismatch. Default None only for "
            "backward resolution of the client-wiring selectors."
        ),
    )
    uv_lock_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Determinism hash (ii) (I4): sha256 of the guardrail service's "
            "resolved services/guardrail/uv.lock (its transitive NeMo + LangChain "
            "+ langchain-aws resolution). A LITERAL pinned string so a drifted "
            "dependency lock forces a different config_sha256 even under a "
            "byte-identical NeMo config. Default None only for backward "
            "resolution of the client-wiring selectors."
        ),
    )


class GuardrailsPin(BaseModel):
    """
    Guardrail policy pin (consumed from Phase 3).

    Phase 3 evolution (system-prompt-leakage input guard): the ``enabled``/
    ``classifier_model_id``/``input_categories`` fields carry backward-
    RESOLUTION defaults so the released pre-guard configs (1.0.0/1.1.0/1.2.0)
    stay resolvable with the guard OFF and are NEVER edited -- and the
    second-slice ``output_categories`` field follows the same rule (empty
    default means the output guard is a typed identity; it is INDEPENDENT of
    ``classifier_model_id`` since the output guard is pure regex with no model
    dependency, so it adds NO validator) — the defaults
    exist for backward RESOLUTION only, exactly like the Phase 2 template/
    context and Phase B history pins. A ``policy_version`` string alone must
    NOT switch behavior: ``enabled`` (plus a pinned classifier id) is the
    actual gate.

    NeMo evolution: the OPTIONAL ``nemo`` selector (:class:`NemoGuardPin`) gates
    the out-of-process NeMo Guardrails OUTPUT + FACTS lane. It defaults to
    ``None`` so the released ``1.0.0``–``1.4.0`` configs resolve with the NeMo
    lane OFF and are NEVER edited (byte-for-byte). Per Q6b the NeMo lane is
    OUTPUT/facts only this slice; the in-house Haiku input confirm-step above is
    untouched.
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
    output_categories: tuple[str, ...] = Field(
        default=DEFAULT_GUARDRAIL_OUTPUT_CATEGORIES,
        description=(
            "Active OUTPUT-guard detector classes (e.g. 'pii', 'secrets'). "
            "Mirrors input_categories and is EXTENSIBLE. Empty default means "
            "check_output is a typed identity (no scan) so released pre-output-"
            "guard configs stay resolvable and behave as today. INDEPENDENT of "
            "classifier_model_id: the output guard is PURE regex with NO model "
            "dependency, so NO validator ties it to a classifier id."
        ),
    )
    nemo: NemoGuardPin | None = Field(
        default=None,
        description=(
            "Optional out-of-process NeMo Guardrails OUTPUT/facts selector. "
            "None (the default) ⇒ the NeMo lane is OFF, so released "
            "1.0.0–1.4.0 configs resolve unchanged and never call the guardrail "
            "pod. Set (with the pod URL supplied via env-driven Settings) to "
            "enable the pod-backed self-check-output + independently-gated "
            "self-check-facts rails. The INPUT lane keeps the in-house Haiku "
            "confirm-step regardless (Q6b)."
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
