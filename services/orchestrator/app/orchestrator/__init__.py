"""
Pipeline stages for the orchestrator's query path.

Every stage is a typed PURE function — the Phase 1 uniform ``run(payload)``
mega-dict signature is gone. Live stages: ``retriever`` (embed + hybrid
search via injected clients), ``context_assembler``, ``prompt_builder``,
``citation_builder``. Parked stages stay in-chain as typed identities on
their natural types: ``policy_router``/``query_rewrite`` on the question,
``reranker`` on the chunk list, ``guardrails`` on input/output text.

Import discipline (enforced by the ``stages-pure`` import-linter contract):
stage modules must NOT import boto3, opensearch-py, or opentelemetry — infra
is confined to ``app.clients`` and INSTANCES are injected by the router.
Stages also never read the environment (grep-gate): behavior comes ONLY from
the pinned ``{name}-{semver}`` config; location facts arrive as arguments.
"""
