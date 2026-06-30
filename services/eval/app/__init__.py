"""Monorepo eval service package.

This is the deliverable that migrates 1:1 to ``genai-backend/services/eval/app/``.
It holds the import-pure ``app.kernel`` core plus the service children
(``app.deepeval``, ``app.phoenix``, ``app.datasets``, ``app.metrics``,
``app.runners``), the packaged JSON ``app.contracts``, the env-driven
``app.config``, and the control-plane skin (``app.api``, ``app.clients``,
``app.schemas``, ``app.main``, ``app.worker``). Local-only tooling lives in the
sibling ``dev/`` package and never ships here.

``import app`` pulls NO deepeval (deepeval is not in sys.modules afterward), so
this package root performs NO import-time os.environ writes: the
DEEPEVAL_TELEMETRY_OPT_OUT opt-out lives in exactly two allowlisted package
__init__ sites (``app.kernel.rag_metrics`` and ``app.deepeval``), each set
before its package imports deepeval.
"""

__version__ = "0.1.0"
