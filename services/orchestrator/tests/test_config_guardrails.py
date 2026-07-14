"""GuardrailsPin schema-extension tests (system-prompt-guardrail Task Group 1).

The guard is CONFIG-GATED: new ``GuardrailsPin`` fields get backward-RESOLUTION
defaults so the released pre-guard configs (1.0.0/1.1.0/1.2.0) keep resolving
with the guard OFF and are NEVER edited. ``enabled=True`` without a pinned
classifier id fails loudly at resolution.

Focused checks only (per task 1.1) — exhaustive per-field permutations are
intentionally skipped.
"""

import pytest
from pydantic import ValidationError


def _pin(**overrides):
    from app.schemas.pipeline_config import GuardrailsPin

    return GuardrailsPin(**overrides)


def test_enabled_true_without_classifier_model_id_is_rejected():
    """The master gate cannot be flipped on without a pinned classifier id."""
    with pytest.raises(ValidationError, match="classifier_model_id"):
        _pin(policy_version="1.0.0", enabled=True)


def test_defaults_keep_released_configs_resolving_with_the_guard_off():
    """1.0.0/1.1.0/1.2.0 resolve with enabled=False via the new schema defaults."""
    from app.config import resolve_pipeline_config

    for ref in (
        "legal-rag-default-1.0.0",
        "legal-rag-default-1.1.0",
        "legal-rag-default-1.2.0",
    ):
        guardrails = resolve_pipeline_config(ref).config.guardrails
        assert guardrails.enabled is False, ref
        assert guardrails.classifier_model_id is None, ref
        # No classifier is consulted when the gate is off.
        assert guardrails.policy_version, ref


def test_input_categories_defaults_to_prompt_leak_when_omitted():
    """The active detector class defaults to the sole prompt_leak class."""
    pin = _pin(policy_version="0.0.0")
    assert pin.input_categories == ("prompt_leak",)


def test_enabled_true_with_a_pinned_classifier_id_resolves():
    """enabled=True + a classifier id is accepted (the 1.3.0 shape)."""
    pin = _pin(
        policy_version="1.0.0",
        enabled=True,
        classifier_model_id="au.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    assert pin.enabled is True
    assert pin.classifier_model_id == "au.anthropic.claude-haiku-4-5-20251001-v1:0"


# ---------------------------------------------------------------------------
# Output-guard gate (second slice, Task Group 1): the new backward-RESOLUTION
# ``output_categories`` field. Empty default keeps 1.0.0-1.3.0 resolvable with
# the OUTPUT guard OFF; the field is INDEPENDENT of classifier_model_id (the
# output guard is pure regex, so NO cross-validator was added).
# ---------------------------------------------------------------------------


def test_output_categories_parses_from_yaml_values_into_a_tuple():
    """A pin can opt a config into the output detector classes."""
    pin = _pin(policy_version="2.0.0", output_categories=["pii", "secrets"])
    assert pin.output_categories == ("pii", "secrets")


def test_output_categories_defaults_to_empty_and_keeps_released_configs_off():
    """A 1.1.0-shaped pin with no output_categories still resolves, guard OFF."""
    pin = _pin(policy_version="0.0.0")
    assert pin.output_categories == ()

    from app.config import resolve_pipeline_config

    for ref in (
        "legal-rag-default-1.0.0",
        "legal-rag-default-1.1.0",
        "legal-rag-default-1.2.0",
        "legal-rag-default-1.3.0",
    ):
        guardrails = resolve_pipeline_config(ref).config.guardrails
        assert guardrails.output_categories == (), ref


def test_output_categories_is_independent_of_classifier_model_id():
    """No cross-validator: output_categories set + guard disabled still resolves."""
    # enabled=False and no classifier id, yet output_categories is populated: the
    # output guard is pure regex with NO model dependency, so this is valid.
    pin = _pin(policy_version="2.0.0", output_categories=["secrets"])
    assert pin.enabled is False
    assert pin.classifier_model_id is None
    assert pin.output_categories == ("secrets",)
