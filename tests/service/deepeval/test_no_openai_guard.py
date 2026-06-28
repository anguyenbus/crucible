"""HARD CI GATE: no OpenAI client is constructed on a Bedrock judge run.

This is the O5/Q8 guard, promoted to a hard CI gate. It lives in the normal
pytest suite so CI runs it on every push and on any deepeval version change.

Contract under test (provider=bedrock, OPENAI_API_KEY UNSET):
- Construct ALL FOUR configured DeepEval metrics (Faithfulness, AnswerRelevancy,
  ContextualPrecision, ContextualRecall) through the real production path
  ``create_deepeval_metrics(llm_provider="bedrock", ...)`` and run ``measure()``
  on a pico test case.
- ``openai.OpenAI`` AND ``openai.AsyncOpenAI`` are monkeypatched to RAISE, so ANY
  accidental OpenAI client construction fails the test loudly.
- The eval succeeds (every metric yields a numeric score) without touching the
  OpenAI path.

Hermeticity (no live AWS): the DeepEval ``AmazonBedrockModel`` is replaced with a
SUBCLASS whose ``generate``/``a_generate`` return canned schema instances. The
subclass is deliberately a subclass of the real ``AmazonBedrockModel`` so that
deepeval's ``is_native_model`` still returns True -- the test therefore exercises
the SAME native measure path the real Bedrock judge uses, just without the
network. The point is to prove no OpenAI path is touched on a Bedrock run, not to
call AWS.

CONTINGENCY (O4->O5): if this guard ever FAILS because an external/OpenAI
embedder path reappears on the resolved deepeval version, re-introduce
CRUCIBLE_JUDGE_EMBEDDER_PROVIDER / _MODEL and wire them to the consuming path
before the spec is complete. Re-run this guard on any deepeval version change.
"""

import deepeval.models as deepeval_models
import openai
from deepeval.metrics.utils import is_native_model
from deepeval.models.llms.amazon_bedrock_model import AmazonBedrockModel
from pydantic import BaseModel

from crucible.kernel.rag_metrics.samples import transform_to_deepeval_sample
from crucible.service.deepeval.bedrock_provider import create_deepeval_metrics

_ALL_FOUR = ("faithfulness", "context_precision", "context_recall", "answer_relevancy")

_RAG_OUTPUT = {
    "query": {"text": "What is the termination clause?"},
    "answer": {"text": "The contract may be terminated with 30 days notice."},
    "retrieved_chunks": [{"text": "Either party may terminate on 30 days written notice."}],
}
_REFERENCE = "The contract may be terminated with 30 days written notice."


class _HermeticBedrockJudge(AmazonBedrockModel):
    """A network-free, OpenAI-free stand-in for the real Bedrock judge.

    Subclasses the real AmazonBedrockModel so deepeval's is_native_model() still
    treats it as a native model (the production path). __init__ is overridden to
    skip the aiobotocore/boto3 wiring; generate/a_generate return canned schema
    instances so each metric can complete measure() with no network call.
    """

    def __init__(self, model: str = "au.anthropic.claude-haiku-4-5-20251001-v1:0", **_: object) -> None:  # noqa: E501
        # Deliberately do NOT call super().__init__: that requires aiobotocore and
        # would build a real Bedrock client. We only need a native-typed model.
        self.model_id = model
        self.model = model

    def load_model(self) -> None:  # pragma: no cover - trivial
        return None

    def get_model_name(self) -> str:
        return self.model_id

    @staticmethod
    def _fill_schema(schema: type[BaseModel]) -> BaseModel:
        """Build a permissive instance of a deepeval response schema."""
        data: dict[str, object] = {}
        for name, field in schema.model_fields.items():
            annotation = field.annotation
            text = str(annotation)
            if "List" in text or "list" in text:
                data[name] = []
            elif annotation is bool:
                data[name] = True
            elif annotation in (float, int):
                data[name] = 1.0
            else:
                data[name] = "yes"
        return schema(**data)

    def generate(self, prompt: str, schema: type[BaseModel] | None = None):
        if schema is not None:
            return self._fill_schema(schema), 0.0
        return "yes", 0.0

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None):
        if schema is not None:
            return self._fill_schema(schema), 0.0
        return "yes", 0.0


def _boom_openai(*_: object, **__: object):  # pragma: no cover - must never run
    raise AssertionError(
        "An OpenAI client was constructed on a Bedrock judge run. The no-OpenAI "
        "guard FAILED: an external/OpenAI path reappeared. Per the O4->O5 "
        "contingency, re-introduce CRUCIBLE_JUDGE_EMBEDDER_* and wire them before "
        "the spec is complete."
    )


def test_no_openai_client_constructed_across_all_four_metrics(monkeypatch):
    """provider=bedrock + OPENAI_API_KEY unset: all four metrics run, no OpenAI."""
    # OPENAI_API_KEY UNSET -- any code reaching for it must not silently succeed.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Region required for the bedrock judge resolution.
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")

    # Any OpenAI client construction fails the test loudly.
    monkeypatch.setattr(openai, "OpenAI", _boom_openai)
    monkeypatch.setattr(openai, "AsyncOpenAI", _boom_openai)

    # Hermetic native Bedrock judge in place of the real AmazonBedrockModel.
    monkeypatch.setattr(deepeval_models, "AmazonBedrockModel", _HermeticBedrockJudge)

    metrics = create_deepeval_metrics(
        llm_provider="bedrock",
        judge_model="au.anthropic.claude-haiku-4-5-20251001-v1:0",
    )

    # The whole configured set is present.
    assert set(metrics.keys()) == set(_ALL_FOUR)

    # Every metric uses the native Bedrock path (matches production), proving the
    # guard exercises the real branch rather than a generic mock.
    for metric in metrics.values():
        assert is_native_model(metric.model) is True

    test_case = transform_to_deepeval_sample(_RAG_OUTPUT, _REFERENCE)

    # Run measure() on ALL FOUR metrics. async_mode is forced off so the native
    # generate() path is exercised synchronously and deterministically.
    for name in _ALL_FOUR:
        metric = metrics[name]
        metric.async_mode = False
        metric.measure(test_case)
        assert metric.score is not None, f"{name} produced no score on a Bedrock run"
        assert isinstance(metric.score, (int, float))


def test_bedrock_judge_construction_never_touches_openai_api_key(monkeypatch):
    """Constructing the bedrock judge never reads/requires OPENAI_API_KEY.

    Bedrock-only: the OpenAI provider was REMOVED, so ``provider=openai`` is now
    rejected outright. The bedrock branch builds with the key unset and OpenAI
    patched to raise -- demonstrating no OpenAI path remains.
    """
    import pytest

    from crucible.service.deepeval.bedrock_provider import get_deepeval_llm

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setattr(openai, "OpenAI", _boom_openai)
    monkeypatch.setattr(openai, "AsyncOpenAI", _boom_openai)
    monkeypatch.setattr(deepeval_models, "AmazonBedrockModel", _HermeticBedrockJudge)

    # openai provider: removed -> rejected outright (no OpenAI path remains).
    with pytest.raises(ValueError, match="Bedrock-only"):
        get_deepeval_llm(provider="openai", model="anything")

    # bedrock branch: builds fine with the key unset and OpenAI patched to raise.
    judge = get_deepeval_llm(provider="bedrock", model="au.anthropic.claude-haiku-4-5-20251001-v1:0")  # noqa: E501
    assert is_native_model(judge) is True
