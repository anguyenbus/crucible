"""Monorepo eval service package.

This is the deliverable that migrates 1:1 to ``genai-backend/services/eval/app/``.
It holds the import-pure ``app.kernel`` core plus the service children
(``app.deepeval``, ``app.phoenix``, ``app.datasets``, ``app.metrics``,
``app.runners``), the packaged JSON ``app.contracts``, the env-driven
``app.config``, and the control-plane skin (``app.api``, ``app.clients``,
``app.schemas``, ``app.main``, ``app.worker``). Local-only tooling lives in the
sibling ``dev/`` package and never ships here.
"""
