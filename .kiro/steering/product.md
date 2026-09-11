---
inclusion: always
---

# Product Overview

Generated from `planpilot_agent_contract_v1.8.json`
(sha256 `b92e53f4ff054105…`). Do not edit by hand; edit the contract and re-run
`generate_kiro_workspace.py`.

## What this is

PlanPilot is an agentic production-planning copilot. It reads structured factory data, validates input quality with record-level quarantine, invokes a deterministic scheduling engine, produces three objective-based alternatives, independently checks hard constraints, explains operational trade-offs and pauses for human approval before consequential actions. Every publish requires explicit Production Planner confirmation.

## Official problem statement (authoritative, verbatim)

Source: NUS-ISS "Show Me Your Agents" Hackathon, Problem Statement Selection —
**Production Planning**.

> Production planners in manufacturing SMEs coordinate customer orders, available inventory, machine capacity, and workforce schedules to determine daily production activities. Planning is often performed manually using spreadsheets and historical experience, making it difficult to adjust quickly to changing customer demand, urgent orders, or material shortages.

`business_context.problem_statement` in the contract is the team's
*interpretation* (team interpretation of the official statement, scoped to a small-batch precision engineering SME for demo concreteness). When the two seem to
conflict, the official statement above wins on scope, the interpretation wins on
demo concreteness.

## Who it is for

**Production Planner** at LionCity Precision Pte. Ltd. — a
fictional Singapore precision engineering SME, high-mix low-volume small-batch manufacturing.

Their goals:
- produce a feasible five-day schedule quickly
- respond to urgent orders and disruptions
- compare multiple business trade-offs
- understand why the Agent recommends a plan
- retain control over consequential changes

Decisions the human keeps (never automate these):
- approve overtime
- approve promised customer due-date changes
- confirm secondary-skill assignments
- select which candidate plan to pursue
- confirm every publication of a production plan

## Pain points being solved

| id | pain point | business impact |
|---|---|---|
| BP-001 | Slow plan generation and replanning | Urgent orders and disruptions consume planner time and delay decisions. |
| BP-002 | Hidden feasibility errors | Machine overlaps, worker conflicts, missing skills or unavailable material can create an executable-looking but impossible plan. |
| BP-003 | Single-plan thinking | Planners cannot easily compare delivery, overtime, changeover and schedule-stability trade-offs. |
| BP-004 | Weak decision traceability | Managers cannot easily see why an operation moved or which assumption caused a delay. |
| BP-005 | Unsafe automation boundaries | An unconstrained AI could invent capacity, approve overtime, change customer commitments or follow malicious text embedded in business data. |

## In scope

- daily planning decisions over a rolling five-day finite-capacity horizon
- production-lot splitting
- machine and worker assignment
- routing precedence
- material availability and confirmed inbound timing
- sequence-dependent changeover handling
- planned maintenance and disruption handling
- overtime proposals
- plan comparison, explanations and approval workflow
- deterministic and reproducible schedule generation (fixed seed)
- record-level quarantine of malformed input data
- prompt-injection defence for untrusted business text

## Out of scope — do not build these

- real ERP or MES writeback during the hackathon prototype
- automatic purchasing or supplier negotiation
- predictive maintenance model training
- foundation-model fine-tuning
- fully autonomous customer due-date commitments
- safety-rule or maintenance overrides

## Coverage of the official statement

Every official phrase maps to concrete artefacts; the full machine-checked map is
`official_statement_alignment` in the contract.

**Four input dimensions** — all covered:
- customer orders: data_source.required_sheets.Orders, $defs.schedule_operation.properties.order_id, recommendation_contract.required_sections.affected_orders
- available inventory: data_source.required_sheets.Inventory, material_consumption_model, HC-005
- machine capacity: data_source.required_sheets.Machines, HC-001, HC-006, HC-011
- workforce schedules: data_source.required_sheets.Workers, data_source.required_sheets.Shift Calendar, data_source.required_sheets.Worker Skills, HC-002

**Three disruption types** — all covered:
- changing customer demand: EVT-006, EVT-007, EVAL-029, EVAL-030, demand_change_model
- urgent orders: EVT-001, EVAL-002
- material shortages: EVT-003, EVAL-004, HC-005, material_consumption_model.validation_codes

**Deliberate non-coverage:**
- historical experience read as demand forecasting — predictive model training and fine-tuning are out of scope; see experience_externalization for the affirmative answer

See `experience_externalization` in the contract for why PlanPilot converts tacit
planner judgement into auditable configuration instead of forecasting demand.

## Hackathon context

- Category: **Public** (individuals not currently employed by or representing an SME, including students)
- Team size: **4** — Public Category teams must consist of exactly four members
- Shortlisting submission: **2026-09-28T09:00:00+08:00**
- Finale face-to-face demo: **2026-10-10T08:30:00+08:00**
- Team Code: `TO_BE_RECORDED_BEFORE_SUBMISSION` ← placeholder, fill before submission

Judging rubric (7 dimensions) — design decisions should be defensible against these:
1. Goal & Scope Definition
2. Architecture & Reasoning Loop
3. Tool Use & Integration
4. Autonomy & Human-in-the-Loop
5. Safety, Security & Guardrails
6. Observability & Evaluation
7. Platform & Tooling Usage

## Success criteria

- **planning_cycle_time**: baseline manual and unmeasured → target validated replan in less than 60 seconds
- **hard_constraint_violation_rate**: baseline manual checking risk → target 0
- **on_time_rate**: baseline legacy value 0.8333333333 used a pre-V1.2 denominator and is not comparable; recompute with the V1.3 eligible-order definition before claiming improvement → target improve the V1.3-comparable on-time rate without reducing eligible-order coverage or using unauthorised overtime
- **plan_stability_after_disruption**: baseline not tracked → target 0.7
- **decision_traceability**: baseline manual explanation → target every recommended change includes reason, KPI impact and approval status

The comparison baseline is `BASE-EDD-001` (Priority EDD).
Its V1.8 metrics are **all null** — `comparison_status` is
`NOT_COMPARABLE_REBASELINE_REQUIRED`. The legacy V1.0 figures are NOT
comparable to current metric definitions and must never be quoted as a result.
