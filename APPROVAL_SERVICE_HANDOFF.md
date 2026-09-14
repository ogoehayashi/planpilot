# Implementation Review Handoff — `approval-service`

**Review target:** annotated tag `approval-service-v1-baseline`  
**Parent baseline:** `c7c06ea89a19c12984aa3d1a9c021a4e4e574f5c`
(`plan-store-and-digest-v1-baseline`)  
**Contract:** `contract/planpilot_agent_contract_v1.8.json`  
**Contract SHA-256:**
`b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

This handoff is for independent review. The contract is authoritative; this
document is only an implementation claim, and
`tools/factcheck_approval_handoff.py` checks its measurable claims against the
timestamped evidence and current tagged tree.

## Outcome

The deterministic core of `approval-service` is implemented and may be reviewed
as the next PlanPilot part. It creates the complete server-derived approval set,
binds every child to immutable plan identity and validator evidence, applies
server-owned expiry, records role-constrained human decisions, enforces publish
preconditions, persists atomically, and consumes PlanStore supersede events in
invalidate-before-ack order.

This does **not** mean PlanPilot is pilot-ready. The public tool adapters,
publisher transaction, UI authentication, audit chain, scheduler, dataset
integration and EVAL-001..030 remain outside this part.

## What changed

- `src/planpilot/approval/`
  - closed contract-derived policy and role mappings;
  - contract-shaped registered approval errors;
  - `ApprovalService` lifecycle, decisions, expiry, persistence and outbox
    consumer.
- `src/planpilot/validation/`
  - cached validation of tool input/output/failure schemas with bundled root
    `$defs`.
- `.kiro/specs/approval-service/`
  - design-first boundary and completed task ledger.
- `tests/unit/test_approval_service.py`
  - 39 focused contract, semantic, failure-atomicity and crash-order tests.
- `tests/negative_control/test_approval_service_negctl.py`
  - 13 mutations, all applied only to a disposable repository copy.
- `tests/evidence/approval-service/`
  - timestamped JSON evidence and raw logs.

## Security and integrity properties under review

1. The request action is only a trigger. It cannot narrow the complete set
   produced from the stored `validate_plan` result.
2. The validation result is schema-checked, digest-bound, feasible and
   hard-violation-free. Its complete KPI object must equal immutable PlanStore
   content.
3. Required overtime and secondary-skill approvals are independently derived
   from those KPIs; publish confirmation is unconditional. Role mappings and
   impact reason codes are closed contract vocabularies.
4. Approval-set and request IDs are deterministic. Reload rejects IDs,
   membership or impact summaries that disagree with stored validation evidence.
5. Expiry uses only supplied server time, contract TTL defaults/min/max, and the
   horizon guard. It never reads a client or workbook clock.
6. Failed persistence rolls prospective in-memory writes back.
7. Supersede processing persists idempotent invalidation before acknowledging
   the PlanStore event. Tests cover crashes before invalidation, after durable
   invalidation before ack, and after ack.

## Reproduced evidence

Generated at `2026-09-14T00:56:07+00:00` by
`tools/write_evidence_approval_service.py`:

| Gate | Result |
|---|---|
| Approval unit suite | **39 passed** |
| Approval mutation control | **13 caught / 0 escaped / 0 broken** |
| Full repository suite | **461 passed** |
| Closed vocabulary guard | **PASS** — 23 vocabularies, 149 members |
| Kiro workspace validation | **ok=190 fail=0** |
| Contract hash | **unchanged** (`b92e53f4…`) |
| `git diff --check` | **PASS** |

Reproduce with the repository Python environment:

```powershell
python tools/write_evidence_approval_service.py
python tools/factcheck_approval_handoff.py
```

On this workstation the interpreter is
`.venv-review\Scripts\python.exe`; the generic `python` WindowsApps alias is not
a usable interpreter in the current shell.

## Explicit design limitations

- `record_validated_plan(...)` is an internal trusted boundary for the future
  deterministic validator adapter. The public `request_approval` schema cannot
  carry required actions, impacts or horizon context and was not expanded.
- Due-date-change approval is accepted from the schema-valid deterministic
  validator result because `plan_content` has no structured field from which
  this service can independently reconstruct promised-date movement. The
  overtime and secondary-skill decisions are independently cross-checked.
- The decision method receives an authenticated identity and role from a future
  UI/auth adapter. This part enforces role authorization but does not authenticate
  the person itself.
- Persistence is a single-process canonical JSON prototype. It is failure-atomic
  for the configured file and in-memory state, not a multi-process database
  transaction claim.
- Existing `plan-store-and-digest` evidence remains historical evidence for its
  own tag. Its old handoff counts are not claims about this later branch.

## Reviewer attack order

1. Try to narrow a set through a different request trigger.
2. Tamper persisted validation KPIs, set membership, IDs and impact summaries.
3. Replay decisions with different identity/time/reason content.
4. Exercise TTL boundaries and rejected/expired/invalidated precedence.
5. Kill at each supersede crash point and verify event convergence.
6. Add a schema-valid but semantically contradictory validation result and check
   whether it can reach a success snapshot.

Recommended verdict wording if the evidence survives review:

> Approved for progression from `approval-service` to the next implementation
> part. This is not approval of runtime EVAL readiness or pilot readiness.
