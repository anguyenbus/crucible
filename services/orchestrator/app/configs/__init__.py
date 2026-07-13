"""
Pinned, immutable pipeline-config artifacts.

Each ``{name}-{semver}.yaml`` file in this package is a released, IMMUTABLE
pipeline config carrying BEHAVIOR pins (generator/embedder model ids, prompt
ref, top_k, retrieval params, guardrail policy version, temperature). Released
configs are never edited — any change requires a NEW ``{name}-{semver}`` file.
Immutability is enforced by the committed ``manifest.sha256`` hash manifest
plus a test that recomputes every hash.

Configs are resolved by ``app.config`` via
``importlib.resources.files("app.configs")`` — exact ``{name}-{semver}`` match
only; no floating refs, no ``latest`` alias, no default-to-newest.
"""
