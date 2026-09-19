# P1-2 Scheduling Engine — Design-First implementation

The V1.8 contract remains authoritative. This design documents the current
implementation boundary and deliberately does not add a competing contract.

## Data flow

1. `runtime_planning._solver_state` expands each order with the pure
   `domain.lots.split_lots` function. The virtual solver key includes lot number;
   the public plan restores the original order and lot identities.
2. `v18_adapter.build_material_reservations` allocates BOM quantities from
   ordered inventory buckets atomically per fixed lot. A shortage releases no
   partial reservation. The independent validator recomputes allocations.
3. `domain.importer` converts declared calendars, changeover pairs and overtime
   limits into solver data. `domain.calendar` handles coverage and overtime
   minutes. V1.8 calendar IDs are authored by the adapter and independently
   checked against both resource calendars.
4. `domain.planning` uses optional CP-SAT intervals, machine circuit arcs for
   exact predecessor-pair setup, hard overtime caps and five lexicographic
   stages. Seed 42 and one search worker are fixed. No-incumbent or exhausted
   stage budgets use a deterministic, whole-order priority dispatch with an
   explicit fallback provenance label.
5. When an explicit baseline is supplied, the solver rewards unchanged
   operation starts only when identity, quantity, type, duration and resources
   match. The adapter computes union-denominator stability; the independent
   validator recomputes it without importing generator code.
6. Runtime authority stores only independently validated, digest-bound
   feasible content. Unscheduled work is surfaced in candidate content, but
   current authority semantics do not store an infeasible candidate.

## Known boundary

The runtime does not yet resolve the most recent published plan across a
changed factory-state ID for the same horizon. An explicit `baseline_plan_id`
can bind the reference. The public chat tool does not yet expose that argument.
The current 0.2 deterministic-unit solver allocation is much smaller than the
contract's shared 40-second escalation ladder; a large-dataset SLA and
fallback-quality claim require separate runtime evidence.
