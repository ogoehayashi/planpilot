---
inclusion: fileMatch
fileMatchPattern: ["src/planpilot/engine/**", "src/planpilot/validation/**", "tests/**"]
---

# Engine Rules — loaded only when touching engine, validation or test code

## Determinism is the product

- **random_seed_default**: 42
- **num_search_workers**: 1
- **iteration_order**: all entity collections sorted by id before model construction
- **search_budget**: Use a fixed CP-SAT deterministic-time, conflict or branch budget for reproducibility; wall-clock time is a safety guard only and must not select the returned incumbent.
- **canonical_serialization**: Sort operations by start_time, machine_id, order_id, lot_no and operation_no; serialize datetimes as RFC 3339 with +08:00. Hash only the immutable plan_content object, excluding plan_content.plan_digest and plan_content.engine.canonical_plan_hash to avoid circularity. Lifecycle and observed runtime fields are separate records and never enter the digest.
- **guarantee**: With the pinned solver build and runtime configuration, identical state_id, ordered profiles, event_id, baseline_plan_id, deterministic search budget and random_seed produce byte-identical canonical operations and KPIs. Runtime measurements are excluded from the equality assertion.
- **verification**: EVAL-009

CP-SAT must be constructed with a fixed `random_seed` and
`num_search_workers=1`. Multi-worker search is non-deterministic in solution
selection even with a seed. `generate_plan_options` accepts `random_seed`
(null → default default seed 42; any fixed seed yields reproducible output"}).

EVAL-009 is the reproducibility test: same input → byte-identical plan. If a
change makes output depend on anything other than declared inputs and the seed,
it fails EVAL-009 even when it looks correct.

## The objective function is lexicographic, not a weighted sum

lexicographic: Tier 0 minimize unscheduled eligible operations; Tier 1 minimize late orders then total tardiness; Tier 2 minimize secondary_skill_assignment_count; Tier 3 maximize the profile-weighted normalized delivery, overtime, changeover and stability score

Profile weights are mathematically operative against documented reference
values. Secondary-skill assignment is **Tier 2** — minimized before profile
score, and never a hard prohibition (see `worker_skill_model.solver_preference`).

## Lot splitting is fixed, not solver-chosen

- mode: `FIXED_MAX_SIZE_DECOMPOSITION`
- rule: lots=ceil(order_quantity/max_lot_size); lots 1..N-1 equal max_lot_size and lot N equals order_quantity-(N-1)*max_lot_size.
- domain: order_quantity and max_lot_size are positive integers in the product's finished-unit UOM.
- reconciliation: lot quantities are positive, no lot exceeds max_lot_size, and their sum exactly equals order_quantity.
- routing: every lot carries the full routing including terminal INSPECTION.
- Optional solver-chosen lot splitting is unsupported. Assumptions.allow_optional_lot_splitting must be absent or false; true is INVALID_INPUT with OPTIONAL_LOT_SPLITTING_UNSUPPORTED.

## Material reservation is ONE deterministic pass, before solving

- Before solving, tentatively reserve every BOM line without double allocation. Commit the lot atomically only when every line is fully covered; otherwise roll back all of that lot's tentative allocations before considering the next lot. READY requires reserved=required for every line and earliest_material_ready_time is the maximum allocated-bucket available_at. SHORTAGE persists allocations=[] and reserved=0 on every line, with readiness=null.
- For each line sum(allocations.quantity_base_units)=reserved_quantity_base_units<=required_quantity_base_units. READY means equality on every line. SHORTAGE means all lines have allocations=[] and reserved=0 after atomic rollback. Across the entire plan, total committed allocation from any source_id never exceeds that bucket's quantity.
- The first PRODUCTION operation of each READY lot has start_time >= earliest_material_ready_time. A SHORTAGE lot and its routing are emitted as unscheduled MATERIAL_SHORTAGE, never passed to the solver as schedulable work.
- validate_plan independently reruns lot and bucket ordering, allocation, readiness and release-time checks; mismatch is MATERIAL_RESERVATION_MISMATCH and over-allocation is HC-005.
- lot priority: Reserve lots in (urgent_flag descending, promised_due_at ascending, order_id ascending, lot_no ascending) order; priority_rank records this total order.
- bucket priority: For each material consume remaining bucket quantities in (available_at ascending, source_type ON_HAND before CONFIRMED_INBOUND at equal time, source_id ascending) order.

### Known accepted limitation — do not "fix" it casually

Reservation is one deterministic pass executed before solving. There is deliberately no post-solve release, re-reservation, second pass or fixed-point iteration: putting reservation inside the solver's search would couple it to search order and destroy the reproducibility and bounded-budget guarantees that scheduling_engine and generation_escalation_policy exist to provide.

**Condition:** A lot can be READY, with material committed and earliest_material_ready_time set, and still end up unscheduled because the solver finds no capacity for it.

**Consequence:** That lot's material stays committed although it will not run, while a lot marked SHORTAGE never reached the solver and might have become feasible with the same material.

**Why accepted:**
- no hard constraint is violated: HC-005 holds, no bucket is double-allocated, the plan remains feasible and valid
- the loss is optimality, not correctness
- the bias is conservative: it under-schedules rather than fabricating capacity
- closing it requires reservation inside solver search, which the determinism design forbids

**Never claim material allocation is optimal in the demo, write-up or any EVAL narrative.**

Closing this requires a bounded post-solve release-and-retry loop
(a bounded post-solve release-and-retry loop with a fixed iteration cap, evaluated separately for determinism).
That is post-hackathon work: it couples reservation to solver search order and
must be re-evaluated for determinism before it is allowed.

## Calendar coverage: intersection of two unions

An operation is feasible only when its entire half-open interval [start_time,end_time) lies in BOTH the union of applicable machine-group windows and the union of applicable worker-group windows.

calendar_window_ids lists every contributing row. Overlapping or exactly adjacent rows may jointly cover an operation; any positive uncovered gap makes HC-007 fail. At least one machine-group and one worker-group row are required.

The operation remains one contiguous interval. It may cross exactly adjacent regular/overtime rows but never an uncovered gap.

`calendar_window_ids` on every operation: minItems 2, maxItems 4.
Exact machine-group and worker-group calendar rows whose unions cover this operation. minItems=2 because coverage needs at least one machine row and one worker row. maxItems=4 is derived, not arbitrary: the operation is one contiguous interval (non_preemption_interaction), the Shift Calendar offers at most one REGULAR and one OVERTIME row per resource type per date, and a contiguous interval can touch at most one regular plus one overtime row per resource type, giving 2 resource types x 2 row kinds = 4. A longer list indicates a calendar modelling error and is rejected by schema rather than silently truncated; genuine coverage failures are reported by HC-007.

## Stability is measured against a union denominator

- unchanged operation: the identity key exists in both plans; product_id, lot_quantity, operation_type, duration_min, machine_id and worker_id are identical; and start_time drift is <= stability_drift_min
- identity key: identity key = (order_id, lot_no, operation_no); compared attributes include product_id and lot_quantity
- The union exposes added/removed keys. A quantity change that preserves lot count is still changed because lot_quantity and duration_min are compared attributes; it cannot inflate unchanged_operations.

A quantity change that preserves lot count is still CHANGED, because
`lot_quantity` and `duration_min` are compared attributes. EVAL-026 pins this;
EVAL-029 exercises it end to end. Do not let stability be flattered by omission.

## Generation escalation: one budget, two rungs

min(generate_plan_options.max_runtime_seconds, 40); all rung time is charged to this one budget

- rung 1 `CP-SAT`: up to 80% of effective_budget, capped at 32 seconds — all requested profiles in canonical order using fixed deterministic sub-budgets
- rung 2 `PRIORITY_DISPATCH_FALLBACK`: all remaining effective_budget, capped at 8 seconds — all requested profiles in canonical order; HEURISTIC_FALLBACK provenance required

return only independently validated candidates; an intermediate rung timeout is internal telemetry, not a tool_error
return non-retryable SEARCH_ESCALATION_EXHAUSTED with the attempted rungs; no partial candidate is returned

An intermediate rung timeout is **internal telemetry, not a tool_error**. The
ladder is not a framework retry and never resets the SLA clock.

## Rolling horizon

The horizon is derived, never stored. It is the five consecutive working days beginning at load_factory_state.as_of_time, resolved against the Shift Calendar. as_of_time is already a required input, so advancing the window needs no new parameter and no engine change.

Each daily planning run passes as_of_time = the current shift start. The window therefore slides forward one working day per run, and operations already published for elapsed days leave the window naturally because they precede as_of_time.

An accepted disruption or demand change triggers a replan for the SAME window (as_of_time unchanged), which is what makes it event-driven rather than only daily. stability_definition measures that replan against the reference plan.

as_of_time must fall inside or before project.planning_horizon for the demo dataset. A run whose derived window exceeds the Shift Calendar rows available is a data gap, reported by validate_factory_state, never silently truncated.

The demo instance is project.planning_horizon (2026-09-14T00:00 to 2026-09-18T23:59 +08:00) is the SINGLE fixed instance used for the hackathon dataset and all EVAL cases, obtained with as_of_time = 2026-09-14T00:00:00+08:00. It is an instance of the rule, not a separate constant.

## Demand changes ride the existing event path

A demand change is an Events row, not a new mechanism. It is authored in the workbook, loaded by load_factory_state, checked by validate_factory_state and applied by generate_plan_options through the existing event_id input. No tool signature changes and no new tool is added.

Applying a demand change re-enters the workflow at RECEIVED and re-runs load_factory_state with a fresh as_of_time, because the Orders sheet itself changed. The existing transition RECEIVED -> DATA_LOADED -> INPUT_VALIDATED -> PLANS_GENERATED carries it; no new state or transition is introduced.

order_id is immutable across a demand change. A quantity revision on the same order_id is a new plan version, never a new order, so stability and traceability remain comparable against the reference plan.

A quantity revision must satisfy lot_splitting_policy.quantity_reconciliation against the NEW order_quantity; a stale decomposition is LOT_QUANTITY_MISMATCH. A due-date pull-in that cannot be met without overtime produces a feasible plan whose approval_requirements include add_overtime, never a silent lateness omission.

A due-date change the Agent would promise back to the customer is change_promised_due_date and requires Production Manager approval. Accepting an inbound customer request to replan is generate_or_simulate_plan and is AUTO_ALLOW; the two directions are distinct and must not be conflated.

## Testing rules

- Tests are runtime evidence. A test that mocks the solver proves nothing about
  feasibility.
- Every EVAL case writes timestamped inputs, outputs, durations, solver build,
  digests, decision traces, approval events and audit-chain records to
  `tests/evidence/`.
- Static schema checks and algorithm smoke tests are SEPARATE evidence classes.
  They never substitute for an EVAL result.
- `unscheduled_operations` must be certified, not silently omitted —
  see `unscheduled_certification`.
