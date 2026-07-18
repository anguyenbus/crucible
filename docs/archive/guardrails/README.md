# Archived guardrail approaches

Superseded guardrail design/planning docs, kept for historical record and
rollback context. **Nothing here is the current plan** — do not implement from
these documents.

The active direction is
[`docs/guardrail-consolidation-plan.md`](../../guardrail-consolidation-plan.md):
consolidate **all** guarding into the NeMo guardrail pod (service consolidation,
not mechanism replacement — the deterministic secrets/PII pieces stay
deterministic, ported into the pod).

## Contents

- **`nemo-guardrails-bedrock-integration-plan.md`** — archived 2026-07-17. The
  *previous* approach: run NeMo as a **parallel shadow lane** next to the
  in-house guards, A/B before flipping, in-house `1.4.0` stays default
  ("measure-and-review"). The consolidation plan retires this split-brain shape.
  > ⚠️ **2026-07-18: the body of this archived doc was LOST** when a subagent's
  > unauthorized `git`/`rm` operations deleted untracked files; only its header
  > and archive banner survive in the session record. It was never committed, so
  > git cannot recover it. If the full text is needed, it must be re-authored.

## Note on live code and its docs

Archiving is **docs only**. The in-house guard code
(`services/orchestrator/app/orchestrator/guardrails.py`) and its reference docs
(`services/orchestrator/docs/guardrails.md`) are **still live** — the
consolidation plan keeps them until its Phase 4 removal window. Those docs stay in
place because they document currently-running code.
