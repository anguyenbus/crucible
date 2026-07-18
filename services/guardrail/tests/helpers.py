"""Shared test doubles for the skeleton tests (mocked LLMRails — no AWS)."""

from __future__ import annotations

from typing import Any

TEST_MODEL_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


class FakeCall:
    """One logged Bedrock call carrying token counts (FINDINGS #3 shape)."""

    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeLog:
    """The ``res.log`` object exposing ``llm_calls``."""

    def __init__(self, calls: list[FakeCall]) -> None:
        self.llm_calls = calls


class FakeRes:
    """A canned ``LLMRails.generate`` result (response turns + token log)."""

    def __init__(
        self, response: list[dict[str, Any]], calls: list[FakeCall] | None = None
    ) -> None:
        self.response = response
        self.log = FakeLog(calls) if calls is not None else None


class FakeRails:
    """A MOCK LLMRails: records calls, returns a preset :class:`FakeRes`."""

    def __init__(self, res: FakeRes) -> None:
        self._res = res
        self.calls: list[dict[str, Any]] = []

    def generate(self, messages: Any, options: Any = None) -> FakeRes:
        self.calls.append({"messages": messages, "options": options})
        return self._res


def benign_res() -> FakeRes:
    """A clean (allowed) generate result with parseable token accounting."""
    return FakeRes(
        response=[{"role": "assistant", "content": "ok"}],
        calls=[FakeCall(prompt_tokens=170, completion_tokens=114)],
    )


def block_res(exc_type: str = "OutputRailException") -> FakeRes:
    """A blocked generate result: a role:'exception' turn (FINDINGS #5)."""
    return FakeRes(
        response=[{"role": "exception", "content": {"type": exc_type}}],
        calls=[FakeCall(prompt_tokens=50, completion_tokens=10)],
    )
