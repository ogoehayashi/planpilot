# Specs

**Use Design-First specs only.**

Requirements are already fixed by `contract/planpilot_agent_contract_v1.8.json`.
A Requirements-First spec would generate a `requirements.md` that competes with
the contract, and then `design.md` and `tasks.md` would be built on the conflict.

Suggested spec decomposition, following `implementation_migration` in the
contract:

| spec | tracks | contract source |
|---|---|---|
| `plan-store-and-digest` | engine | `plan_store`, `$defs.plan_content`, `$defs.plan_lifecycle` |
| `tool-error-middleware` | engine | `tool_execution_contract`, 18 error codes + details schemas |
| `audit-hash-chain` | engine | `security_controls.audit_log_integrity`, `observability` |
| `approval-service` | engine | `approval_set_lifecycle`, `approval_set_invariants`, `approval_expiry_policy`, `approval_rules` |
| `decision-traces` | engine | `observability`, `$defs.decision_trace_record` |
| `dataset-migration` | dataset | `implementation_migration.dataset`, `data_source.dataset_requirements_note` |
| `lot-and-material` | engine | `lot_splitting_policy`, `material_consumption_model` |
| `calendar-and-shifts` | engine | `shift_and_overtime_model` |
| `cpsat-scheduler` | engine | `scheduling_engine`, `hard_constraints`, `objective_function` |
| `escalation-and-budget` | engine | `generation_escalation_policy`, `time_budget_policy` |
| `independent-validator` | engine | `validate_plan`, `unscheduled_certification`, `kpis` |
| `inference-client` | platform | `platform_binding.llm_inference`, `provider_portability`, cost ceilings |
| `eval-harness` | evidence | `acceptance_tests` (EVAL-001..030), `implementation_migration.evidence` |
| `ui-approval-queue` | engine | `approval_channels`, `recommendation_contract` |
| `lightsail-deploy` | platform | `implementation_migration.platform` |

The first three and the approval service are unblocked today — they need no
dataset. `dataset-migration` is the critical path for everything else.
