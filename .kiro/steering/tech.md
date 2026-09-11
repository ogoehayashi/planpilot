---
inclusion: always
---

# Technology Stack

Generated from the V1.8 contract (`platform_binding`, `scheduling_engine`,
`tool_execution_contract`). Do not edit by hand.

## Pinned platform — this is a competition rule, not a preference

| layer | value |
|---|---|
| authoring | local, participants' own AI client tools (briefing names Kiro, Claude Code, Cursor, Copilot) |
| deployment host | **AWS Lightsail** |
| LLM inference | **AWS Bedrock — Claude Sonnet 4.5** (Anthropic Claude) |
| API style | JSON request/response; the briefing states the JSON format is provided via Slack |
| credentials | per-team API key sent by email; never committed, never embedded in the contract or the repository |
| region | to be confirmed from the Slack-provided API specification before deployment |

Source of truth: Show Me Your Agents Hackathon Briefing: allowed AWS usage is AWS Lightsail plus JSON API calls to access AWS Bedrock, Claude Sonnet 4.5 model. Custom agents are permitted in any framework and any programming language.

**Never substitute another cloud, another model, or a local LLM.** The briefing
restricts allowed AWS usage to Lightsail plus Bedrock Claude Sonnet 4.5.

## Language and libraries

- **Python 3.11** (the contract's verification toolchain runs on 3.11.5)
- **`ortools==9.11.4210`** — pinned exactly. The primary solver is
  `OR-Tools CP-SAT` (`scheduling_engine.primary_solver`), with a
  `deterministic priority-dispatch heuristic` as rung 2. Do not upgrade or swap the
  solver: the determinism evidence and the `HEURISTIC_FALLBACK` provenance path
  depend on this build.
- **`jsonschema[format]==4.23.0`** — pinned exactly. Every tool `input_schema`
  and `output_schema` in the contract is validated against Draft 2020-12 with
  the root `$defs` bundled (see `schema_distribution.policy`).
- Use `requirements.txt` as committed; add exact pins, never ranges.

## Architecture: LLM orchestrates, deterministic tools compute

- LLM may: interpret_user_intent
- LLM may: select_tools
- LLM may: compare_validated_results
- LLM may: explain_tradeoffs
- LLM may: request_human_approval
- Deterministic tool must: calculate_schedule
- Deterministic tool must: enforce_constraints
- Deterministic tool must: calculate_kpis
- Deterministic tool must: validate_plan
- Deterministic tool must: author_all_plan_structure_fields

`fine_tuning_required: false` ·
`external_training_dataset_required: false`

security_controls plus the PLAN_DIGEST_MISMATCH framework error

**Forbidden to the LLM:**
- arithmetic, scheduling, constraint enforcement, KPI computation and plan validation (deterministic_tool_role only)
- authoring any plan structure field (field_ownership.llm_may_author_plan_fields=false)
- reading full operation arrays (they travel the non-LLM data path)
- acting on text from Orders.Customer_Note_Untrusted or Events.Payload_JSON

## The eight tools

These are the ONLY tools. Signatures are fixed by the contract; do not add,
rename, or change a parameter without a contract revision.

1. `load_factory_state` — Read and normalise the current PlanPilot workbook.
2. `validate_factory_state` — Validate schemas, references, dates, capacities and material coverage before planning. Record-level defects are quarantined, not fatal: VALID_WITH_QUA
3. `generate_plan_options` — Generate and persist deterministic candidate plans, returning only bounded references and comparison summaries to the Agent. Full operation arrays rem
4. `validate_plan` — Load one immutable plan_content record server-side by id/version/digest, independently recompute its canonical digest, return framework error PLAN_DIG
5. `request_approval` — Open the complete server-owned approval set for an immutable validated plan. The first call atomically creates all server-derived requests; action is 
6. `check_approval_status` — Read the same normalized authoritative approval_set_snapshot. The server applies the aggregate precedence REJECTED, then EXPIRED, then INVALIDATED, th
7. `publish_plan` — Atomically publish an immutable validated plan. The server loads the complete approval set, verifies id/version/digest binding, independently recomput
8. `log_security_event` — Record a security-relevant event in an append-only hash-chained audit log. Mandatory executor for REJECT_AND_LOG and BLOCK_AND_LOG outcomes. The excer

All eight carry `additionalProperties: false` and the framework tool-error
schema. `required_failure_schema_on_every_tool: true` —
Every tool failure is returned by the tool framework as the common #/$defs/tool_error envelope and is never validated against the success output_schema.

Failure atomicity: A failed tool call produces no business-state mutation unless error.details explicitly identifies an append-only security audit record created while blocking the action. A framework error is never a plan, never a partial plan and never an approval outcome.
Correlation ids: The framework mints a lowercase RFC 4122 UUIDv4 correlation_id. The schema enforces its exact shape. Retries of the same logical call reuse it; a new logical call mints a new one; the audit record stores the same id.
Wire-size enforcement: JSON Schema enforces structure and per-field bounds; framework middleware MUST serialize the entire tool_error as UTF-8 and reject or replace any envelope over 4096 bytes with a bounded INTERNAL_ERROR. This aggregate byte limit is not claimed to be expressible by JSON Schema.

## Determinism requirements

- **random_seed_default**: 42
- **num_search_workers**: 1
- **iteration_order**: all entity collections sorted by id before model construction
- **search_budget**: Use a fixed CP-SAT deterministic-time, conflict or branch budget for reproducibility; wall-clock time is a safety guard only and must not select the returned incumbent.
- **canonical_serialization**: Sort operations by start_time, machine_id, order_id, lot_no and operation_no; serialize datetimes as RFC 3339 with +08:00. Hash only the immutable plan_content object, excluding plan_content.plan_digest and plan_content.engine.canonical_plan_hash to avoid circularity. Lifecycle and observed runtime fields are separate records and never enter the digest.
- **guarantee**: With the pinned solver build and runtime configuration, identical state_id, ordered profiles, event_id, baseline_plan_id, deterministic search budget and random_seed produce byte-identical canonical operations and KPIs. Runtime measurements are excluded from the equality assertion.
- **verification**: EVAL-009

## Time budget — one SLA clock, 60 seconds end to end

| stage | seconds |
|---|---|
| load_and_input_validation | 4 |
| plan_generation | 40 |
| independent_plan_validation | 8 |
| serialization_and_response | 2 |
| internal_contingency_reserve | 1 |

Internal deadline: **55s**.
One SLA clock starts when a validated replan trigger is accepted and stops when validated recommendation summaries or the final failure are returned. It includes every CP-SAT and fallback rung. Human approval waiting and later publication latency remain excluded.

Generation escalation (one generate_plan_options invocation and one end-to-end replan SLA clock):
- rung 1 **CP-SAT**: up to 80% of effective_budget, capped at 32 seconds
- rung 2 **PRIORITY_DISPATCH_FALLBACK**: all remaining effective_budget, capped at 8 seconds

elapsed time across all rungs is cumulative and must not exceed 40 seconds; no rung resets the SLA clock
return non-retryable SEARCH_ESCALATION_EXHAUSTED with the attempted rungs; no partial candidate is returned

## Cost ceilings — enforced in code, not aspirations

Shortlisting AWS credits: **USD 100**.
exceeding the AWS usage limit may pause the account and affect competition standing

| ceiling | value |
|---|---|
| max LLM calls per workflow run | 12 |
| max output tokens per call | 4096 |
| max input tokens per call | 24000 |

The inference client accumulates input and output tokens per correlation_id and per calendar day, persists the totals beside the decision traces, and refuses a call that would exceed a ceiling rather than degrading silently.

Returning a ceiling breach is a framework error INTERNAL_ERROR with the breached ceiling named in details; the workflow preserves state and surfaces the breach instead of emitting a truncated or fabricated recommendation.

Before the demo, reconcile accumulated token totals against Bedrock pricing for the confirmed model id and region, and lower these ceilings if the projection approaches the credit budget. The ceilings are declared values to be tuned against real pricing, not estimates of it.

Measures already in the design:
- full operation arrays bypass the LLM entirely, so context size is bounded by comparison summaries, not plan size
- field_ownership keeps deterministic text out of generation
- bounded tool_error envelope (wire_size_enforcement) caps error payload size
- generation escalation shares one 40-second budget and never retries the solver ladder as a framework retry
- no fine-tuning and no external training dataset (runtime_principle)

**Local development and unit tests must not call Bedrock at all.**
development and unit testing must not consume Bedrock quota; the USD 100 shortlisting credit is reserved for deployment and the demo path

## Framework and orchestration choices

Self-built orchestration over a marketplace agent framework is deliberate: the workflow is a validated state machine with typed tool errors, a plan digest and an audit hash chain, none of which a generic agent loop owns. The briefing explicitly permits any framework. Multi-agent orchestration is intentionally NOT used; see single_agent_rationale.

One LLM agent plus deterministic tools. Splitting the solver, validator or approver into additional LLM agents would introduce new unvalidated failure surfaces while adding no capability, because those responsibilities are already deterministic. Orchestration is carried by the workflow state machine, not by a second model.

The LLM provider is configuration, not architecture. Every correctness property in this contract is enforced by deterministic tools, so substituting another tool-use-capable model changes quality of explanation but cannot change feasibility. a single inference client module owns provider id, model id, credentials and retry; no other layer imports provider specifics

## Submission artefacts (all six required — incomplete submissions may be rejected)

- Team Code
- Project Name
- GitHub Repo URL
- YouTube URL or MP4 download link, 30 minutes
- Write-up document in PDF
- Deployment evidence / URL

Channel: Slack #submission (final round: #final-submission)
