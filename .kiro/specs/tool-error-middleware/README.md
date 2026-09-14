# `tool-error-middleware` — teammate entry point

This directory is the self-contained handoff for the next PlanPilot part.

Read and use the files in this order:

1. `START_PROMPT.md` — copy the whole prompt into a new Codex/Kiro task;
2. `design.md` — the implementation design and contract mapping;
3. `tasks.md` — the only execution checklist for this part.

The authoritative requirements remain
`contract/planpilot_agent_contract_v1.8.json`. This directory deliberately has
no `requirements.md`: PlanPilot uses Design-First specs because the contract is
already complete.

Starting repository state:

- work from the current clean branch tip;
- verify that `approval-service-v1.0.1-hardening^{}` resolves to
  `0cf1060990e4668bc293744d0e44e9baf3aff20b` and is an ancestor;
- create branch `tool-error-middleware`;
- never move or overwrite any existing baseline tag.

This part stops at an external-review handoff. It does not implement the eight
public tool handlers, publisher, audit hash chain, decision-trace persistence,
scheduler, dataset migration, UI, Bedrock client, or EVAL-001..030.
