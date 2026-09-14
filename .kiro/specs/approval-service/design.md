# Design — approval-service

**Mode:** Design-First  
**Contract:** `contract/planpilot_agent_contract_v1.8.json`  
**Contract sha256:** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

The contract is the requirements document. This spec implements
`approval_set_lifecycle`, `approval_set_invariants`, `approval_expiry_policy`,
`approval_rules`, `approval_channels`, `approval_polling_policy`, the approval
`$defs`, and the approval-related security controls. It does not introduce a
second requirements file or change a public tool schema.

## 1. Boundary and ownership

`ApprovalService` is deterministic server code. It never calls an LLM and never
trusts workbook text or the public `request_approval.action` trigger to describe
what approvals are required.

The public tool inputs remain exactly those in the contract. Before
`request_approval` can succeed, the future `validate_plan` adapter records its
latest successful result through an internal boundary:

```python
record_validated_plan(
    validation_result,       # exact validate_plan success output
    impact_by_action,        # deterministic validator-derived approval_impact
    horizon_end,             # server-side planning context
    validated_at,
)
```

This internal record is bound to `(plan_id, plan_version, plan_digest)` and
checked against `PlanStore`. It exists because none of `approval_requirements`,
per-action impact, or the planning horizon is present in the public
`request_approval` input. Letting the caller supply those fields there would
violate `caller_cannot_narrow`, `expiry_is_server_owned`, and the fixed tool
schema.

## 2. Required-action derivation

The authoritative set is the action set in the stored `validate_plan` result,
subject to service-side cross-checks:

- `publish_plan` is always present;
- `add_overtime` is present exactly when recomputed `kpis.overtime_hours > 0`;
- `assign_qualified_secondary_skill` is present exactly when recomputed
  `kpis.secondary_skill_assignment_count > 0`;
- `change_promised_due_date` is carried by the deterministic validator result,
  because `plan_content` has no structured promised-due-date-change field;
- each action has the contract role and one validated `approval_impact`;
- the validator's complete KPI object is byte-canonically equal to the immutable
  plan content KPI object;
- no duplicate or extra requirement/impact is accepted.

The trigger action is validated against the four public actions but never filters
this set. All requests are built in memory, semantically validated, and then
committed as one complete set.

## 3. Identity and idempotency

Approval-set and request ids are deterministic SHA-256-derived identifiers of the
immutable binding and action. Repeating `request_approval` for the same binding,
with either the same or a different trigger action, returns the existing set.

Every child repeats the exact set id, plan id, version and digest. Lifecycle
records and returned snapshots are defensive copies. Approval decisions never
rewrite identity or impact fields.

## 4. Server-owned expiry

All times are injected RFC 3339 timestamps with an explicit offset. The service
never reads the wall clock.

For every action:

```text
lower = server_now + 300 seconds
upper = min(server_now + 172800 seconds, horizon_end + 24 hours)
candidate = proposed_expires_at or server_now + action_default_ttl
effective_expires_at = min(max(candidate, lower), upper)
```

If `upper < lower`, creation fails atomically with
`APPROVAL_WINDOW_CLOSED`. Pending children become `EXPIRED` when an authoritative
read or publish precondition is evaluated at/after their expiry.

## 5. Decisions and aggregate precedence

Only authenticated UI integration may call the internal decision method. The
service enforces action-to-role mapping. `APPROVED` requires no reason;
`REJECTED` requires one member of `approval_decision_reason_code`; comment and
actor fields retain the contract bounds.

Aggregate status is recomputed, never trusted:

```text
any REJECTED   -> REJECTED
else any EXPIRED -> EXPIRED
else invalidated -> INVALIDATED
else all APPROVED -> APPROVED
else PENDING
```

Publication precondition evaluation converts non-approved outcomes to the
registered errors `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`,
`APPROVAL_SET_INVALIDATED`, `APPROVAL_SET_INCOMPLETE`, or `APPROVAL_REQUIRED`.

## 6. Supersede outbox and crash order

`PlanStore` owns a durable at-least-once supersede outbox. The consumer order is:

1. validate the event and approval-set binding;
2. invalidate the set idempotently;
3. atomically persist approval state;
4. acknowledge the PlanStore event.

A crash before step 2 leaves the event pending. A crash after step 3 but before
step 4 replays the event; invalidation is idempotent, so the replay only retries
the acknowledgement. An event naming a missing or differently bound set is never
acknowledged.

## 7. Persistence

Approval state is canonical JSON written through same-directory temporary-file
replacement. The persisted shape is module-owned and closed. On load, contract
schemas plus semantic checks reject incomplete membership, duplicate actions,
wrong child bindings, wrong roles, contradictory decision fields, bad times, or
unknown invalidation causes. Reload also re-runs the validator-evidence checks
and requires deterministic ids and child impacts to match the stored validated
record. A failed persistence call rolls its prospective in-memory mutation back;
callers never observe state that failed to reach the configured durable file.

This is a single-process prototype store, not a claim of database-grade
multi-process transactions. The later publish/tool layer must coordinate its own
atomic write boundary and audit hash-chain event.

## 8. Explicitly not in this spec

- the public tool adapters for `request_approval` and `check_approval_status`;
- `publish_plan` and publish idempotency;
- UI authentication and the Approval Queue panel;
- audit hash-chain records and decision traces;
- scheduler/validator implementation and EVAL execution.

No result from this spec changes
`release_readiness.runtime_evaluation=PENDING_UNTIL_EVAL_001_TO_030_EXECUTE`.
