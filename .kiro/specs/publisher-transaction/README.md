# `publisher-transaction` — teammate entry point

This directory is the self-contained Design-First handoff for G2: the
contract-level publisher with a strict single-transaction publish path.

Read and use the files in this order:

1. `START_PROMPT.md` — copy the whole prompt into a new Codex/Kiro task;
2. `design.md` — the implementation design and contract mapping;
3. `tasks.md` — the only execution checklist for this part.

The authoritative requirements remain
`contract/planpilot_agent_contract_v1.8.json`. This directory deliberately has
no `requirements.md`: PlanPilot uses Design-First specs because the contract is
already complete. This spec is independent of `tool-error-middleware`; the
middleware is reused as a component here, not merged into that spec.

Starting repository state (verified 2026-09-20):

- **Feature baseline (audit-approved):** annotated tag `g2-baseline-5bf299a`
  → `5bf299a`. Do NOT move or rewrite this tag, and do NOT checkout the tag
  to start work.
- **Actual branch parent for G2 development:** `c1e9274` (append-only devlog
  successor of `5bf299a`; it registers the G1.0.2 approval and the deferred
  P2 `clock_session` defence-in-depth item). Work starts from the current
  clean `p1-3-hardening` tip.
- Inherited counts to re-record, not copy: unit `642 passed`, full `649
  passed`, negctl `7 passed` with `RESTORE-MISMATCH: none`, contract SHA-256
  prefix `b92e53f4…fe639`.

Gate: this spec ships as a design checklist for reviewer approval. No
implementation task may start until the reviewer approves `design.md` and
`tasks.md`, and the first implementation slice is explicitly
`idempotency_registry + IDEMPOTENCY_CONFLICT`.
