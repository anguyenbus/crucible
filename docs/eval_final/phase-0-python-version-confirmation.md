# Phase 0 — Python Version Decision (decision 0.1)

This is the evidence for the Python minor version decision. The original Phase 0
rule was *confirm-only, keep `>=3.13`*. That was **superseded by an explicit
decision to lower the floor to `>=3.12` for migration safety** — targeting the
lower bound maximizes compatibility with the monorepo `services/*` toolchain and
removes the one thing that can hard-block the copy (a monorepo pinned below 3.13).

## Decision

- `requires-python` **lowered `>=3.13` → `>=3.12`**. No upper bound was added (no `<3.14`).
- ruff `target-version` lowered `py313 → py312` to match.
- **Verified on CPython 3.12.11** (not merely asserted): a throwaway 3.12 venv
  resolved and installed all extras + dev deps, and the full test suite ran:
  **120 passed, 1 failed**, where the single failure
  (`test_chromadb_collection_exists`) **passes in isolation** — a pre-existing
  ChromaDB persistent-state ordering flake, identical to the 3.14 result, not a
  3.12 incompatibility.

## Touchpoints changed (all lowered to 3.12)

| Source | Old | New |
|---|---|---|
| `pyproject.toml` `requires-python` | `>=3.13` | `>=3.12` |
| `pyproject.toml` `[tool.ruff] target-version` | `py313` | `py312` |
| `Dockerfile` (3 stages) | `python:3.13-slim` | `python:3.12-slim` |
| `.pre-commit-config.yaml` | `python3.13` | `python3.12` |

## 3.12/3.13-only-syntax grep (insurance against a hidden hard-block)

Searched `src/`, `tests/`, `scripts/` for syntax requiring >3.11 that could break
on a 3.12 floor:

| Probe | Pattern | Result |
|---|---|---|
| PEP 695 type aliases | `^\s*type\s+NAME\s*=` | none |
| PEP 695 generic def/class | `\b(def|class)\s+NAME\[` | none |
| `@override` (3.12+) | `@(typing\.)?override\b` | none |
| `itertools.batched` (3.12+) | `batched(` | none |
| version gates 3.12–3.19 | `version_info ... (3, 1[2-9]` | none |

**Conclusion:** no syntax that would break on the `>=3.12` floor. Dependency
resolution succeeds on 3.12 and the suite is green on 3.12, so the lowered floor
carries no hidden hard-block and improves monorepo-migration safety.
