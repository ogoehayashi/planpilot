# Tasks — approval-service

**Design:** `./design.md` · **Contract:** `contract/planpilot_agent_contract_v1.8.json`

## Phase 1 — contract boundary and policy

- [x] **1.1 Add tool-payload schema validation**
      Compile the existing contract tool input/output schemas with the bundled
      root `$defs`; do not duplicate approval schemas in production code.
- [x] **1.2 Implement `approval/policy.py`**
      Pin the four approval actions, role mapping, reason mapping, action order,
      expiry defaults and min/max TTL to the contract. Add drift tests.
- [x] **1.3 Implement contract-shaped approval errors**
      Cover `APPROVAL_REQUIRED`, `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`,
      `APPROVAL_WINDOW_CLOSED`, `APPROVAL_SET_INVALIDATED`,
      `APPROVAL_SET_INCOMPLETE`, and `STATE_NOT_FOUND`.

## Phase 2 — lifecycle service

- [x] **2.1 Record one validated-plan result per immutable binding**
      Verify PlanStore content/digest, exact validate_plan output, feasible result,
      required-action semantics, roles and per-action impacts.
- [x] **2.2 Create complete approval sets atomically and idempotently**
      Trigger action never narrows the set. Return defensive, schema-valid
      `approval_set_snapshot` records.
- [x] **2.3 Enforce server-owned expiry**
      Per-action defaults, min/max clamping, horizon guard, fail-closed window,
      deterministic injected clock.
- [x] **2.4 Record authenticated human decisions**
      Role mapping, controlled rejection reason, idempotent same-decision retry,
      immutable identity/impact, aggregate precedence.
- [x] **2.5 Enforce publish preconditions**
      Convert every incomplete/rejected/expired/invalidated/pending state into its
      exact registered error details shape.

## Phase 3 — persistence and PlanStore outbox

- [x] **3.1 Canonical atomic persistence and semantic reload validation**
- [x] **3.2 Idempotently consume supersede events**
      Persist invalidation before acknowledging the PlanStore event.
- [x] **3.3 Test the three crash points**
      Before invalidation, after durable invalidation before ack, and after ack;
      retries must converge without duplicate side effects.

## Phase 4 — evidence and adversarial controls

- [x] **4.1 Add focused unit/adversarial tests**
- [x] **4.2 Add approval-service mutation tests in a disposable repo copy**
- [x] **4.3 Write timestamped module evidence and an external review handoff**
- [x] **4.4 Commit a clean approval-service baseline**
