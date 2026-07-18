"""Gated NeMo default-flip test.

The Chainlit default pinned config ref would flip to the nemo-all
``legal-rag-default-1.8.0`` (every guard VERDICT through the out-of-process pod)
ONLY on a green parity gate and the USER's go/no-go (PENDING). Until then the
default STAYS ``legal-rag-default-1.4.0`` and the flip is a ONE-LINE
``DEFAULT_PIPELINE_CONFIG`` swap — no code change.

This file pins BOTH states so the flip is unambiguous and revertible:

  * the CURRENT default is asserted green as ``1.4.0`` (the flip has not happened);
  * the POST-FLIP state is an ``xfail`` documenting exactly what one line changes.

The post-flip target is the nemo-all ``1.8.0`` — the sole active NeMo config any
future flip would adopt (the retired ``1.5.0``-``1.7.0`` stepping stones are gone).

Focused check only — browser / E2E skipped.
"""

from __future__ import annotations

import pytest
from chat_ui import config

_IN_HOUSE = "legal-rag-default-1.4.0"
_NEMO = "legal-rag-default-1.8.0"


def test_default_config_ref_still_1_4_0_pending_gated_flip():
    """PENDING USER GO/NO-GO: the default stays ``1.4.0`` until the gate is green.

    Flipping to ``1.8.0`` is a single ``DEFAULT_PIPELINE_CONFIG`` line swap —
    gated on the parity gate + the USER's go/no-go, NOT applied here.
    """
    assert config.DEFAULT_PIPELINE_CONFIG == _IN_HOUSE
    assert config.pipeline_config_ref() == _IN_HOUSE


@pytest.mark.xfail(
    reason="Gated flip PENDING USER GO/NO-GO after the live parity gate; the "
    "default stays 1.4.0 until then. This documents the one-line post-flip state "
    "(nemo-all 1.8.0, the sole active NeMo reference).",
    strict=False,
)
def test_default_config_ref_is_1_8_0_after_the_gated_flip():
    """After the gated flip the UI queries the nemo-all lane (``1.8.0``) by default."""
    assert config.DEFAULT_PIPELINE_CONFIG == _NEMO
    assert config.pipeline_config_ref() == _NEMO
