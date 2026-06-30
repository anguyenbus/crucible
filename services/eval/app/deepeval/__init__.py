"""DeepEval judge provider + embeddings (service-side, impure)."""

import os

# ====================================================================
# SECURITY: DISABLE THIRD-PARTY TELEMETRY (service allowlisted line)
# ====================================================================
# DeepEval phones home unless DEEPEVAL_TELEMETRY_OPT_OUT is set. Set it HERE,
# at the package __init__, before anything in this package imports deepeval
# (bedrock_provider.py imports deepeval LAZILY inside functions at ~:225/:230/
# :273, but importing any service.deepeval submodule runs this __init__ first,
# so the opt-out is structurally set before any deepeval code can run).
# Evaluation runs may contain sensitive query data; telemetry to external
# servers is a privacy / compliance hazard.
#
# This is one of exactly TWO os.environ writes permitted in app library
# code (the other is app.kernel.rag_metrics.__init__). The redundant
# writes in the package root, bedrock_provider.py, and the dev/cli/*
# shells were removed in Phase 4 -- both allowlisted package __init__ sites set
# the opt-out before their package's deepeval import, which is the whole
# safety basis. DO NOT REMOVE OR MODIFY.
#
# Reference: https://docs.confident-ai.com/docs/telemetry-opt-out
# ====================================================================
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"
