# Approval Service Hardening Report — Zombie Set Invalidation

**Date:** 2026-09-14  
**Parent approval baseline:** `069f9f8` / `approval-service-v1-baseline`  
**Target tag after commit:** `approval-service-v1.0.1-hardening`  
**Contract:** `planpilot_agent_contract_v1.8.json`  
**Contract SHA-256:** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

## Finding

An approval set can be created and fully approved while the corresponding
PlanStore lifecycle is still `DRAFT`. If a new plan version is then committed,
the supersede event truthfully carries `approval_set_id=null`, because the
lifecycle was never linked to the set. The original consumer treated null as
"no approval exists", acknowledged the event and left the real set reporting
`APPROVED` forever.

PlanStore still prevented publication of the superseded version, so this was
not a publication bypass. It was nevertheless a direct approval-lifecycle
semantic defect: regeneration must invalidate every prior set, and UI/audit
must not display a zombie approved set.

## Change

`ApprovalService.consume_superseded()` now resolves the set through both
authoritative paths:

1. the event's lifecycle-derived `approval_set_id`, when present;
2. otherwise the approval service's own immutable
   `(plan_id, plan_version, plan_digest) -> approval_set_id` binding index.

If both paths exist but name different sets, processing fails closed with
`ApprovalInvariantError`. A fallback-resolved set follows the same durable
ordering as every other set: validate binding, persist idempotent invalidation,
then acknowledge the original PlanStore event. The acknowledgement retains the
event's original null set id so PlanStore's outbox binding remains exact.

## Regression and mutation evidence

The new regression reproduces the full reported sequence through public APIs:

```text
v1 lifecycle DRAFT
→ create complete approval set
→ approve every request
→ commit v2
→ observe supersede event approval_set_id=null
→ consume event
→ old set aggregate_status=INVALIDATED
→ require_approved raises ApprovalSetInvalidatedError
```

An additional negative-control mutation deletes only the binding fallback. The
real approval unit suite detects it, proving the new test is coupled to the
protection rather than merely exercising adjacent behaviour.

Measured results on the pre-commit tree at `2026-09-14T01:42:14Z`:

| Gate | Result |
|---|---|
| Approval unit suite | **40 passed** |
| Approval mutation control | **14 caught / 0 escaped / 0 broken** |
| Full repository suite | **462 passed** |
| Closed vocabulary guard | **PASS — 23 vocabularies** |
| `git diff --check` | **PASS** |

## Tag finding disposition

No repair was made to `plan-store-and-digest-v1-baseline`, because it was never
mispointed. It is an annotated tag:

```text
tag object: f8b5af7b2b16ee13c8bde0b67031b2e64478a92c
peeled commit: c7c06ea89a19c12984aa3d1a9c021a4e4e574f5c
```

Comparing the tag-object SHA directly with a commit SHA created the earlier
false alarm. The correct command is:

```powershell
git rev-parse "plan-store-and-digest-v1-baseline^{}"
```

Force-moving that tag would rewrite an already audited baseline and would be a
new process defect. The original tag is therefore intentionally unchanged.

## Scope

This closes the zombie approval-set finding only. The original
`approval-service-v1-baseline` tag remains immutable historical evidence for
commit `069f9f8`; the new hardening tag identifies this corrective commit.
Due-date-change provenance still depends on the future deterministic validator
adapter and remains an explicit integration requirement. This report does not
claim tool-layer, publisher, EVAL or pilot readiness.
