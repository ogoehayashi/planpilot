# AGENTS.md

PlanPilot AI — Production Planning agent for the NUS-ISS "Show Me Your
Agents" Hackathon (Public category).

## Read these first, in this order

1. `.kiro/steering/contract-authority.md` — the contract is the single source of
   truth; closed vocabularies; what you may not claim
2. `.kiro/steering/guardrails.md` — approval ladder, injection defence, publishing gates
3. `.kiro/steering/tech.md` — pinned platform (Lightsail + Bedrock Claude Sonnet 4.5), determinism, cost ceilings
4. `.kiro/steering/product.md` — official problem statement, persona, scope
5. `.kiro/steering/structure.md` — module boundaries and naming rules
6. `contract/planpilot_agent_contract_v1.8.json` — the authoritative text (sha256 `b92e53f4ff054105…`)

## Non-negotiable rules

- **The contract wins.** It is a finished specification, not a draft to improve
  while coding. Never invent a validation code, error code, tool, parameter,
  KPI, profile, state or event id.
- **The LLM never computes.** Scheduling, constraints, KPIs and validation come
  only from deterministic tools. The LLM interprets intent, selects tools,
  compares validated results, explains trade-offs and requests approval.
- **Determinism or it did not happen.** Fixed seed, single search worker, no
  dict-order or wall-clock dependence.
- **Never hide bad news.** Late orders, unscheduled operations, hard violations,
  required approvals and solver infeasibility are always surfaced.
- **No Bedrock calls in local development or tests.** Credits are USD 100 for the whole shortlisting round.
- **Never commit credentials.**
- **Nothing is "done" without timestamped runtime evidence.** No EVAL case has
  been executed yet; `runtime_evaluation` is
  `PENDING_UNTIL_EVAL_001_TO_030_EXECUTE`.

## Spec workflow: Design-First only

Requirements already exist — they are the contract. A Requirements-First spec
would generate a competing `requirements.md`. Use **Design-First** so specs
produce `design.md` and `tasks.md` that *implement* the contract.

## Verifying a change

```bash
python tools/generate_kiro_workspace.py   # steering follows the contract
python -m pytest tests/unit               # deterministic, no Bedrock
```

The contract's own check suite lives beside the contract:

```bash
cd <contract dir>
python verify_contract.py            # 600 static assertions
python independent_audit_v18.py      # 398 independent checks
python negative_control_v18.py       # 8 mutations, all must be caught
python engine_semantic_diff.py       # zero unintended engine drift
```

Those scripts validate the CONTRACT. They do not validate your implementation.
Implementation evidence comes from `tests/eval/` writing to `tests/evidence/`.

## Regenerating steering

`.kiro/steering/*.md` and this file are **generated** by
`tools/generate_kiro_workspace.py` from the contract. Hand-edits are lost on the
next run. To change agent guidance, change the contract, then regenerate.
