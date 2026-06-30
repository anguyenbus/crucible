"""
RAG/judge metric specs and DeepEval evaluator (pure kernel).

This package holds the pure metric specifications (default judge/generator IDs,
the metric-name registry, thresholds, and ``assert_distinct``) plus the DeepEval
sample transform and the inverted ``DeepEvalEvaluator``.
"""

import os

# ====================================================================
# SECURITY: DISABLE THIRD-PARTY TELEMETRY (kernel allowlisted line)
# ====================================================================
# DeepEval phones home unless DEEPEVAL_TELEMETRY_OPT_OUT is set. Set it HERE,
# before anything in this package imports deepeval (samples.py / evaluator.py
# import deepeval lazily, but this guards eager importers too). Evaluation runs
# may contain sensitive query data; telemetry to external servers is a privacy /
# compliance hazard.
#
# This is the ONE os.environ write permitted inside app.kernel: it is
# explicitly allowlisted by the kernel grep-gate pre-commit hook. Phase 4
# collapsed the opt-out to exactly TWO library sites -- this kernel line and
# app.deepeval.__init__ -- each set before its package imports
# deepeval. import-linter cannot see attribute access, so the grep-gate is what
# allows exactly this line.
# ====================================================================
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"
