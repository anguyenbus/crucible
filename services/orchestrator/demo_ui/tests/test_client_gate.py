"""Readyz-gate + error-detail parsing tests."""

import asyncio
import json

from chat_ui.client import _error_detail, check_ready


def test_readyz_gate_reports_real_error_when_orchestrator_unreachable():
    # Nothing listens on this port: the gate must report the real transport
    # failure (never a degraded fake mode) so on_chat_start shows it and stops.
    ready, detail = asyncio.run(check_ready("http://127.0.0.1:9"))

    assert ready is False
    assert "unreachable" in detail
    assert "127.0.0.1:9" in detail


def test_error_detail_joins_pydantic_422_list_body():
    # FastAPI/Pydantic 422 bodies carry a LIST of field errors — the UI must
    # show the human `msg` fields, not a raw Python repr of the list.
    body = json.dumps(
        {
            "detail": [
                {"loc": ["body", "pipeline_config"], "msg": "string does not match pattern"},
                {"loc": ["body", "history", 0, "role"], "msg": "input should be 'user'"},
            ]
        }
    ).encode()

    detail = _error_detail(422, body)

    assert detail == "string does not match pattern; input should be 'user'"
    assert "loc" not in detail and "[{" not in detail


def test_error_detail_uses_string_detail_directly():
    body = json.dumps({"detail": "Unknown pipeline_config reference."}).encode()
    assert _error_detail(404, body) == "Unknown pipeline_config reference."
