"""Grep-gate: Flow A (PhoenixAdapter span tracing) must stay deleted from ``src/``.

After the Flow A retirement (``adapter.py`` deleted, the ``phoenix=`` param +
span block stripped from ``run_golden_set``), this gate fails the build if any of
the retired surface area creeps back into ``src/``:

  * ``PhoenixAdapter`` references,
  * a ``phoenix=`` keyword parameter / argument,
  * any import of ``service.phoenix.adapter``.

It runs in the default hermetic suite (no Phoenix server, no extra) -- it only
reads source text under ``src/``.
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo-root/src resolved from this file: tests/service/phoenix/ -> repo root.
_SRC_ROOT = Path(__file__).resolve().parents[3] / "src"


def _src_py_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def test_src_has_no_phoenix_adapter_reference():
    """No ``PhoenixAdapter`` token survives anywhere in ``src/``."""
    offenders = [p for p in _src_py_files() if "PhoenixAdapter" in p.read_text()]
    assert not offenders, f"PhoenixAdapter still referenced in: {offenders}"


def test_src_has_no_adapter_import():
    """Nothing imports the deleted ``service.phoenix.adapter`` module."""
    pattern = re.compile(r"service\.phoenix\.adapter")
    offenders = [p for p in _src_py_files() if pattern.search(p.read_text())]
    assert not offenders, f"service.phoenix.adapter imported in: {offenders}"


def test_src_has_no_phoenix_kwarg():
    """No ``phoenix=`` keyword parameter or argument remains in ``src/``.

    The ``phoenix=`` wiring on ``run_golden_set`` (and its call sites) was the
    Flow A coupling; it must not reappear.
    """
    pattern = re.compile(r"\bphoenix\s*=")
    offenders: list[str] = []
    for p in _src_py_files():
        for i, line in enumerate(p.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{p}:{i}: {line.strip()}")
    assert not offenders, "phoenix= kwarg/param found:\n" + "\n".join(offenders)
