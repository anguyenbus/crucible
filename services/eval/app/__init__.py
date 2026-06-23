"""Monorepo eval service package (migration-rehearsal destination).

This is a minimal committed package so ``uv lock`` has something to resolve. The
rehearsal script (``scripts/rehearse_migration.sh``) populates this package by
copying crucible's ``kernel/`` and ``service/`` children into ``app/`` and
rewriting imports; the generated subpackages (``app.kernel``, ``app.deepeval``,
``app.phoenix``, ``app.datasets``, ``app.metrics``, ``app.runners``,
``app.config``, ``app.contracts``) are gitignored.
"""
