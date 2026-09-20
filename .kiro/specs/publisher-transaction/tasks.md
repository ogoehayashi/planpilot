# Tasks — `publisher-transaction`

**Design:** `./design.md`

**Contract:** `contract/planpilot_agent_contract_v1.8.json`
**Start prompt:** `./START_PROMPT.md`

Do tasks in order. A checked box means code plus tests exist and were run; it
does not mean "planned" or "partially implemented".

REVIEWER GATE: no task below Phase 0 may start until the reviewer approves
this file and `design.md`. Phase 0 is verification-only (no code).

## Phase 0 — baseline and contract pinning (no implementation)

- [ ] **0.1 Verify the starting repository**
      Clean worktree on `p1-3-hardening`; HEAD = `c1e9274` (parent
      `5bf299a`); `git rev-parse g2-baseline-5bf299a^{}` equals
      `5bf299adb1e06c2f061db4086cc3bf183944ff56`; the tag is NOT moved or
      rewritten. Record all four facts in the first commit message.
- [ ] **0.2 Re-record the inherited suite (no copying)**
      Full unit run (inherited: 642 passed), full suite (inherited: 649
      passed), negctl (inherited: 7 passed, `RESTORE-MISMATCH: none`). Raw
      stdout stored, hashes taken from committed blobs.
- [ ] **0.3 Pin the publish contract surface in tests**
      New test: contract `tools[publish_plan]` input/output/failure schemas,
      `idempotency_key` bounds (16–128), `IDEMPOTENCY_CONFLICT`
      non-retryable, details `$defs.error_details_idempotency_conflict`
      required fields — asserted directly against V1.8 bytes (contract SHA
      `b92e53f4…fe639` unchanged).

## Phase 1 — tables + idempotency probe (FIRST CODE SLICE, reviewer-named)

- [ ] **1.1 `publication_receipt` + `idempotency_registry`**
      Exact DDL from design §3 appended to the `Database.__init__`
      executescript block; both tables only
      reachable under `Database.lock`/`transaction()`. Unit tests: schema
      shape, UNIQUE constraints (`tool_name+key`, `plan_id+version`), FK
      enforcement ON.
- [ ] **1.2 Canonical request fingerprint**
      `sha256(canonical(request))` over the exact 5 contract fields using
      `persistence.canonical` (same function as audit chain). Test:
      key-order-independence + any-field-change sensitivity.
- [ ] **1.3 `PublisherService` skeleton + idempotency probe**
      Steps 3–4 of design §4 only: open txn, SELECT registry, hit+fingerprint
      match → replay stored response (from receipt, verbatim);
      hit+different fingerprint → `IdempotencyConflictError` (publish
      variant, contract details shape, non-retryable) → rollback.
      No lifecycle/audit writes yet.
- [ ] **1.4 Replay/conflict tests (cases A/B)**
      Include two-connection same-key concurrent barrier test (case 6/7 of
      §8 pre-wired on probe-only path) and zero-change assertions:
      registry/receipt/audit/lifecycle untouched on conflict.

## Phase 2 — first-publication transaction (steps 5–12)

- [ ] **2.1 Revalidation + approval precondition inside txn**
      Digest/version/lifecycle/validator re-checks → contract error codes
      (cases F matrix); `require_approved` wired.
- [ ] **2.2 Lifecycle → PUBLISHED, capture real audit id**
      Transition via existing authority core inside the SAME transaction;
      `_audit(...)` return value captured into a local (the G1 gap).
      `plan_published` audit event stays the only new event.
- [ ] **2.3 Response build + output-schema gate + receipt/registry insert**
      Steps 9–11; strict output validation BEFORE commit; failure → full
      rollback (§8 case 9 test lands here).
- [ ] **2.4 Single COMMIT + `/publish` route rewiring**
      api_server `/publish` → `ToolErrorMiddleware.execute` with a
      `PreparedToolCall` over `PublisherService` (prepare = steps 3–11 inside
      open txn, commit = step 12, rollback = release). Transition core runs
      INSIDE the open txn (never re-opens BEGIN IMMEDIATE); already-PUBLISHED
      binding never re-transitions, `ApprovalSetReplaySafeError` is the
      replay path, anything else fails closed (design §4/§7 step 7).
      Remove the discarded-`_audit` publish path; legacy `publish_plan`
      stays as internal core called by the publisher (no duplicate state
      machines).
- [ ] **2.5 Alias semantics (case C) + fail-closed second set (case D)**
      New key + same binding + same approval set → stored receipt + alias
      registry row, zero new audit; different approval set →
      `IDEMPOTENCY_CONFLICT`, `original_status:"PUBLISHED"`.

## Phase 3 — crash matrix (blocking evidence, design §8)

- [ ] **3.1 Fault-injection harness**
      Deterministic raise-after-step hook in `PublisherService` (test-only
      seam), restart = reopen the same file (G1.0.2 clock discipline:
      persisted `clock_session`, no re-anchor).
- [ ] **3.2 Crash points 1–5**
      Before-lifecycle / after-lifecycle-before-audit / after-audit-before-
      receipt / after-receipt-before-commit / after-commit-before-response;
      each asserts the exact persisted-state vector (lifecycle, audit count
      + `verify_audit()`, revision, receipt, registry).
- [ ] **3.3 Concurrency matrix (6–7) + restart replay (8)**
      Two open `Database`s + two `PublisherService`s, barrier-synced, same
      key; same key different payload; genuine restart replay.
- [ ] **3.4 Output-validation rollback proof (9) + audit identity (10–11)**
      Mutation-injected bad output → full rollback; exact retry same
      `audit_log_id`, entry_count unchanged; retry adds zero `plan_published`.
- [ ] **3.5 Negative zero-change matrix (12)**
      non-planner, expired/rejected/invalidated approval, stale version,
      digest mismatch, approval-window-closed → contract errors, all four
      persisted layers untouched.

## Phase 4 — suite, evidence, review handoff

- [ ] **4.1 Full regression**
      Targeted → clock/evidence regressions → full unit → full suite →
      negctl + restore hashes. No inherited count may drop.
- [ ] **4.2 Evidence pack**
      `tests/evidence/g2-publisher-transaction/` + `EVIDENCE.json` with
      hashes from final committed LF blobs; `test_evidence_integrity.py`
      green in main repo AND harness copy (no-.git path).
- [ ] **4.3 Devlog entry (append-only)**
      Record actual counts (never copied), the case-C alias decision +
      rationale, case-D fail-closed decision, and this spec's gate state.
- [ ] **4.4 Review handoff**
      Clean `git diff --check`, contract SHA unchanged, tag
      `g2-baseline-5bf299a` unmoved, commit list since `c1e9274`, bundle +
      `bundle verify` from a temp clone, one-page report. STOP for external
      review — do not start G3.
