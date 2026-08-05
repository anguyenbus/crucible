"""``POST /compare`` — two-document contradiction detection (LangChain mocked).

The route builds its own ``ChatBedrockConverse`` via the ``_build_chat_model``
seam, so these tests monkeypatch that seam with a fake structured model and
never touch AWS. They prove: the decompose-then-verify flow returns typed
contradictions, degenerate/duplicate rows are dropped, a parse failure degrades
honestly to an empty list (never 500), and a genuine Bedrock ``ClientError``
still maps to the app-level 502.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.routers import compare as compare_module
from app.routers.compare import _AtomicClaim, _ClaimSet
from app.schemas.compare import Contradiction, ContradictionReport
from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.exceptions import OutputParserException


class _FakeStructured:
    """Stand-in for ``model.with_structured_output(schema)``.

    Dispatches on the requested schema: the decompose stage asks for
    ``_ClaimSet`` (``.batch``), the verify stage for ``ContradictionReport``
    (``.invoke``).
    """

    def __init__(
        self,
        schema: Any,
        claim_sets: list[_ClaimSet],
        report: ContradictionReport,
        *,
        error: Exception | None = None,
    ) -> None:
        self._schema = schema
        self._claim_sets = claim_sets
        self._report = report
        self._error = error

    def batch(
        self,
        messages: list,
        config: dict | None = None,
        return_exceptions: bool = False,
    ) -> list:
        if self._error is not None:
            if return_exceptions:
                return [self._error for _ in messages]
            raise self._error
        return self._claim_sets

    def invoke(self, messages: list) -> ContradictionReport:
        if self._error is not None:
            raise self._error
        return self._report


class _FakeModel:
    def __init__(
        self,
        claim_sets: list[_ClaimSet],
        report: ContradictionReport,
        *,
        error: Exception | None = None,
    ) -> None:
        self._claim_sets = claim_sets
        self._report = report
        self._error = error

    def with_structured_output(self, schema: Any) -> _FakeStructured:
        return _FakeStructured(schema, self._claim_sets, self._report, error=self._error)


def _install(monkeypatch, model: _FakeModel) -> None:
    monkeypatch.setattr(
        compare_module, "_build_chat_model", lambda *a, **k: model
    )


def _claims(*pairs: tuple[str, str]) -> _ClaimSet:
    return _ClaimSet(claims=[_AtomicClaim(claim=c, quote=q) for c, q in pairs])


def test_compare_returns_typed_contradictions(client, monkeypatch):
    report = ContradictionReport(
        contradictions=[
            Contradiction(
                type="temporal",
                description="Different start dates.",
                quote_a="Starts Jan 15",
                quote_b="Starts end of Q1",
            ),
            Contradiction(
                type="numerical",
                description="Surplus vs deficit.",
                quote_a="$12M surplus",
                quote_b="$5M deficit",
            ),
        ]
    )
    _install(
        monkeypatch,
        _FakeModel(
            [_claims(("A starts Jan 15", "Starts Jan 15")), _claims(("B starts Q1", "Starts end of Q1"))],
            report,
        ),
    )

    response = client.post(
        "/compare", json={"document_a": "doc a text", "document_b": "doc b text"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] == "au.anthropic.claude-sonnet-4-6"
    assert body["truncated"] is False
    assert [c["type"] for c in body["contradictions"]] == ["temporal", "numerical"]
    assert body["contradictions"][0]["quote_a"] == "Starts Jan 15"
    assert body["contradictions"][0]["quote_b"] == "Starts end of Q1"


def test_compare_drops_degenerate_and_duplicate_rows(client, monkeypatch):
    report = ContradictionReport(
        contradictions=[
            # Valid.
            Contradiction(
                type="process", description="Route conflict.", quote_a="via HR portal", quote_b="through admins"
            ),
            # Missing quote_b -> dropped (not a real, showable conflict).
            Contradiction(type="process", description="half", quote_a="x", quote_b="   "),
            # Exact duplicate of the first -> dropped.
            Contradiction(
                type="process", description="Route conflict.", quote_a="via HR portal", quote_b="through admins"
            ),
        ]
    )
    _install(monkeypatch, _FakeModel([_claims(("c", "q")), _claims(("d", "r"))], report))

    body = client.post(
        "/compare", json={"document_a": "a", "document_b": "b"}
    ).json()

    assert len(body["contradictions"]) == 1
    assert body["contradictions"][0]["quote_a"] == "via HR portal"


def test_compare_no_claims_short_circuits_to_empty(client, monkeypatch):
    # Both documents decompose to nothing substantive -> nothing can contradict,
    # and the verify stage must not even be consulted.
    def _boom_invoke(self, messages):  # pragma: no cover - must not be called
        raise AssertionError("verify must be skipped when there are no claims")

    monkeypatch.setattr(_FakeStructured, "invoke", _boom_invoke)
    _install(monkeypatch, _FakeModel([_ClaimSet(), _ClaimSet()], ContradictionReport()))

    body = client.post("/compare", json={"document_a": "a", "document_b": "b"}).json()
    assert body["contradictions"] == []


def test_compare_parse_failure_degrades_to_empty(client, monkeypatch):
    # The model RAN but returned no valid structured output (a parse failure) —
    # this degrades honestly to an empty list, never a 500.
    _install(
        monkeypatch,
        _FakeModel([], ContradictionReport(), error=OutputParserException("no tool call")),
    )

    response = client.post("/compare", json={"document_a": "a", "document_b": "b"})
    assert response.status_code == 200
    assert response.json()["contradictions"] == []


def test_compare_one_document_parse_failure_preserves_the_other(client, monkeypatch):
    # Document A fails to parse but B decomposes fine: B's claims must NOT be
    # discarded — the verify stage still runs and can surface a contradiction.
    report = ContradictionReport(
        contradictions=[
            Contradiction(type="numerical", description="d", quote_a="1", quote_b="2")
        ]
    )
    good_b = _claims(("B says two", "the number is two"))

    class _MixedStructured:
        def batch(self, messages, config=None, return_exceptions=False):
            return [OutputParserException("A: no tool call"), good_b]

        def invoke(self, messages):
            return report

    class _MixedModel:
        def with_structured_output(self, schema):
            return _MixedStructured()

    _install(monkeypatch, _MixedModel())

    body = client.post("/compare", json={"document_a": "a", "document_b": "b"}).json()
    assert len(body["contradictions"]) == 1


def test_compare_infra_fault_does_not_masquerade_as_no_contradictions(client, monkeypatch):
    # A botocore network fault is NOT a parse failure: it must NOT be swallowed
    # into an empty "documents agree" result (which would fabricate a clean bill
    # of health for a call that never ran). It propagates instead.
    class _NetworkDown(BotoCoreError):
        fmt = "could not connect to the endpoint"

    _install(monkeypatch, _FakeModel([], ContradictionReport(), error=_NetworkDown()))

    with pytest.raises(BotoCoreError):
        client.post("/compare", json={"document_a": "a", "document_b": "b"})


def test_compare_bedrock_client_error_maps_to_502(client, monkeypatch):
    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse"
    )
    _install(monkeypatch, _FakeModel([], ContradictionReport(), error=err))

    response = client.post("/compare", json={"document_a": "a", "document_b": "b"})
    assert response.status_code == 502
    assert response.json()["dependency"] == "bedrock"


def test_compare_truncates_long_documents_and_reports_it(client, monkeypatch):
    captured: dict[str, Any] = {}

    class _Capturing(_FakeModel):
        def with_structured_output(self, schema):
            structured = super().with_structured_output(schema)
            original = structured.batch

            def _batch(messages, config=None, return_exceptions=False):
                captured["messages"] = messages
                return original(messages, config, return_exceptions=return_exceptions)

            structured.batch = _batch  # type: ignore[method-assign]
            return structured

    long_a = "x" * 30_050  # over the 30_000-char per-doc cap
    _install(
        monkeypatch,
        _Capturing([_claims(("c", "q")), _claims(("d", "r"))], ContradictionReport()),
    )

    body = client.post(
        "/compare", json={"document_a": long_a, "document_b": "short"}
    ).json()

    assert body["truncated"] is True
    # The decompose prompt for A carries exactly the 30_000-char slice.
    doc_a_user_msg = captured["messages"][0][1]["content"]
    assert "x" * 30_000 in doc_a_user_msg
    assert "x" * 30_001 not in doc_a_user_msg


def test_compare_empty_document_is_422(client):
    assert client.post("/compare", json={"document_a": "", "document_b": "b"}).status_code == 422
