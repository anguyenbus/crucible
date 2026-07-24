"""HARD CI GATE: the pod never fires NeMo's anonymous usage beacon.

``LLMRails.__init__`` calls ``nemoguardrails.telemetry.report_usage``, which POSTs
a usage event to an NVIDIA endpoint and starts a heartbeat daemon thread, unless
an opt-out is active. This pod guards legal / accounting traffic: NO unsolicited
egress, regardless of how anonymous the payload claims to be.

Three acceptance-blocking checks — a FAILURE fails the slice, and this gate must
be re-run on any NeMo version change:

1. The env-var names the pod sets are the names NeMo actually reads. Asserted
   against the INSTALLED ``nemoguardrails/telemetry.py`` source, so a NeMo
   upgrade that renames or drops a var fails HERE rather than silently
   re-enabling the beacon in production.
2. The deploy surface (``Dockerfile``) sets both vars, and
   :func:`app.bedrock_engine.disable_usage_telemetry` sets them in-process
   without overwriting an operator's explicit value — defence in depth, so a
   dropped ENV cannot silently re-enable it.
3. End-to-end: a REAL ``LLMRails`` built through the production
   ``bedrock_engine.build_rails`` path never reaches the telemetry send. The
   thread-spawn helper is monkeypatched to RAISE, so any surviving code path
   fails loudly instead of quietly opening a socket.

Note on check 3: NeMo ALSO suppresses telemetry when ``pytest`` is in
``sys.modules``, so the raising sentinel would not fire under pytest even with
the opt-out removed. That is precisely why check 1 exists — it pins the
production behaviour, which pytest's own suppression would otherwise mask.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.bedrock_engine import _TELEMETRY_OPT_OUT, build_rails, disable_usage_telemetry

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _SERVICE_ROOT / "Dockerfile"
_FIXTURE_CONFIG = str(Path(__file__).resolve().parent / "fixtures" / "nemo_config")


def _nemo_telemetry_source() -> str:
    """The INSTALLED NeMo telemetry module source (the code that actually runs)."""
    from nemoguardrails import telemetry

    return inspect.getsource(telemetry)


def test_opt_out_var_names_still_match_nemo() -> None:
    """
    Every var the pod sets is one NeMo's opt-out predicate reads.

    If a NeMo upgrade renames these, the pod would keep setting dead vars and
    start beaconing. Pin the coupling to the real source.
    """
    source = _nemo_telemetry_source()
    for name in _TELEMETRY_OPT_OUT:
        assert name in source, (
            f"{name!r} no longer appears in nemoguardrails/telemetry.py — NeMo "
            "changed its usage-telemetry opt-out. The pod may now be beaconing. "
            "Re-read `_is_usage_stats_enabled` and update _TELEMETRY_OPT_OUT."
        )

    assert "_is_usage_stats_enabled" in source, (
        "nemoguardrails/telemetry.py no longer defines `_is_usage_stats_enabled` "
        "— the opt-out mechanism this gate relies on has moved."
    )


def test_dockerfile_sets_both_opt_out_vars() -> None:
    """The deploy surface disables the beacon even if the code path is bypassed."""
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    for name in _TELEMETRY_OPT_OUT:
        assert f"{name}=1" in dockerfile, (
            f"Dockerfile does not set {name}=1 — the container would fire NeMo's "
            "usage beacon on every LLMRails construction."
        )


def test_disable_usage_telemetry_sets_vars_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sets the vars when absent; NEVER contradicts an explicit operator value."""
    for name in _TELEMETRY_OPT_OUT:
        monkeypatch.delenv(name, raising=False)

    disable_usage_telemetry()

    import os

    for name, value in _TELEMETRY_OPT_OUT.items():
        assert os.environ[name] == value

    # An operator's explicit value survives — the pod reports, it does not lie.
    monkeypatch.setenv("DO_NOT_TRACK", "0")
    disable_usage_telemetry()
    assert os.environ["DO_NOT_TRACK"] == "0"


def test_build_rails_never_starts_a_telemetry_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The production construction path reaches no telemetry send.

    Constructing ``LLMRails`` is offline (it builds the boto3 client but makes NO
    Bedrock call), so this asserts the real path without touching AWS.
    """
    from nemoguardrails import telemetry

    def _boom(*_: object, **__: object):  # pragma: no cover - must never run
        raise AssertionError(
            "NeMo started a usage-telemetry thread on the guardrail pod. The "
            "no-egress gate FAILED: the opt-out did not take. Check "
            "app.bedrock_engine.disable_usage_telemetry and the Dockerfile ENV."
        )

    monkeypatch.setattr(telemetry, "_start_daemon_thread", _boom, raising=True)

    rails = build_rails(_FIXTURE_CONFIG)
    assert rails is not None
