"""I5 — model-role separation (acceptance-blocking): NeMo guard id ≠ generator id.

The NeMo ``type: main`` model (the guard's decision LLM, Haiku) MUST be a
DISTINCT model from the pipeline's generator (Sonnet) — the guard role never
doubles as the answer generator (and, by the same token, is not the eval judge).
This holds NOW, before any default flip: the NeMo lane already ships in
``services/guardrail/config/config.yml`` and the in-house ``1.4.0`` generator pin
already exists, so the separation is provable independently of the default.

Focused check only (per task 6.1) — browser / E2E skipped.
"""

from __future__ import annotations

from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]
# services/orchestrator/tests -> parents[2] == services -> the sibling pod.
GUARDRAIL_CONFIG_YML = SERVICE_ROOT.parent / "guardrail" / "config" / "config.yml"


def _nemo_main_model_id() -> str:
    """Read the NeMo ``type: main`` model id from the guardrail pod's config."""
    config = yaml.safe_load(GUARDRAIL_CONFIG_YML.read_text())
    mains = [m for m in config["models"] if m.get("type") == "main"]
    assert len(mains) == 1, f"expected exactly one `type: main` model, got {len(mains)}"
    return mains[0]["model"]


def test_nemo_guard_model_id_differs_from_generator_model_id():
    """I5: the NeMo guard LLM (Haiku) is NOT the pipeline generator (Sonnet)."""
    from app.config import resolve_pipeline_config

    nemo_main = _nemo_main_model_id()
    generator = resolve_pipeline_config("legal-rag-default-1.4.0").config.generator.model_id

    # Acceptance-blocking inequality: guard role stays separate from generation.
    assert nemo_main != generator
    # And they are the intended DISTINCT roles (Haiku guard vs Sonnet generator).
    assert "haiku" in nemo_main.lower()
    assert "sonnet" in generator.lower()


def test_nemo_guard_model_matches_the_in_house_input_classifier_role():
    """The NeMo guard shares the Haiku *guard* role with the in-house classifier.

    Both guard seams (in-house input classifier + NeMo self-check) run Haiku;
    neither is the Sonnet generator. This pins the role split, not a coincidence.
    """
    from app.config import resolve_pipeline_config

    nemo_main = _nemo_main_model_id()
    guardrails = resolve_pipeline_config("legal-rag-default-1.4.0").config.guardrails

    # The NeMo guard and the in-house input classifier occupy the SAME guard role
    # (both Haiku), distinct from the generator.
    assert nemo_main == guardrails.classifier_model_id
    assert nemo_main != resolve_pipeline_config(
        "legal-rag-default-1.4.0"
    ).config.generator.model_id
