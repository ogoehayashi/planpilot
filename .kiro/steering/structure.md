---
inclusion: always
---

# Project Structure

Module boundaries follow the contract's own sections, so a reviewer can map any
file back to the clause that requires it.

## Layout

```
PlanPilot-build/
├── AGENTS.md                     # Kiro also reads this standard
├── .kiro/
│   ├── steering/                 # GENERATED - do not hand-edit
│   ├── hooks/                    # event-driven verification
│   └── specs/                    # Kiro specs (design-first)
├── contract/
│   └── planpilot_agent_contract_v1.8.json   # READ-ONLY authoritative source
├── tools/
│   └── generate_kiro_workspace.py           # regenerates steering from contract
├── src/planpilot/
│   ├── inference/                # THE ONLY module that knows about Bedrock
│   │   └── bedrock_client.py     #   provider id, model id, key, retry, ceilings
│   ├── tools/                    # one module per contract tool (8 total)
│   │   ├── load_factory_state.py
│   │   ├── validate_factory_state.py
│   │   ├── generate_plan_options.py
│   │   ├── validate_plan.py
│   │   ├── request_approval.py
│   │   ├── check_approval_status.py
│   │   ├── publish_plan.py
│   │   └── log_security_event.py
│   ├── engine/
│   │   ├── lots.py               # lot_splitting_policy (fixed decomposition)
│   │   ├── materials.py          # material_consumption_model (pre-solve reservation)
│   │   ├── calendar.py           # shift_and_overtime_model (unions + intersection)
│   │   ├── solver_cpsat.py       # scheduling_engine, rung 1
│   │   ├── fallback_dispatch.py  # rung 2, HEURISTIC_FALLBACK provenance
│   │   ├── escalation.py         # the single 40s shared budget
│   │   ├── stability.py          # stability_definition (union denominator)
│   │   └── kpis.py               # the anti-gaming KPI set
│   ├── store/
│   │   ├── plan_store.py         # plan_content (immutable) + plan_lifecycle
│   │   ├── digest.py             # canonical JSON digest
│   │   └── trace.py              # decision_trace_record writer
│   ├── approval/
│   │   ├── service.py            # approval_set_lifecycle + invariants
│   │   └── policy.py             # approval_rules ladder
│   ├── audit/
│   │   └── hash_chain.py         # append-only SHA-256 chain, genesis sentinel
│   ├── workflow/
│   │   └── state_machine.py      # the 11 states, 20 transitions
│   ├── validation/
│   │   ├── factory_state.py      # 36 validation codes
│   │   └── hard_constraints.py   # HC-001..HC-013
│   ├── security/
│   │   └── untrusted.py          # quarantine, injection defence
│   └── ui/                       # Approval Queue panel + NON-LLM Gantt data path
├── data/
│   └── PlanPilot_Mock_Factory_Dataset.xlsx   # 17 sheets
├── tests/
│   ├── unit/
│   ├── eval/                     # EVAL-001..030
│   └── evidence/                 # timestamped runtime artefacts
└── deploy/                       # Lightsail
```

## Naming and import conventions

- One module per contract tool, named exactly as the tool. No aliasing.
- Deterministic code must not import from `inference/`. The dependency is one
  way: `inference/` may call tools, tools must never call the LLM.
  This is what makes `The LLM provider is configuration, not architecture` true.
- Every module that emits a validation failure uses a code from the contract's
  `validation_issue_code` enum. **Never invent a new code** — if none fits, that
  is a contract-change request, not a coding decision.
- Every framework error uses one of the 18 registered codes with its exact
  `details` schema from `tool_execution_contract.details_schemas`.
- Timestamps are RFC 3339 with `+08:00` (`Asia/Singapore`).
- Money/quantity arithmetic on materials uses **integer base units**
  (EA/G/ML) per `material_consumption_model.quantity_system`. No floats.

## Data path rule

Full operation arrays never enter an LLM context. `plan_store` holds them;
`ui/` renders them directly. Only bounded comparison summaries and references
cross into `inference/`. This is a cost control AND the reason
`field_ownership.llm_may_author_plan_fields` can be false.

## Trace and audit

Every tool invocation writes one `decision_trace_record`
(22 properties,
11 required) via the
framework middleware — not via a tool. Traces join the audit hash chain. An EVAL
result must be reconstructable from traces alone.
