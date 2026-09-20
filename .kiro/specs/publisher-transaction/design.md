# Design — `publisher-transaction`

**Mode:** Design-First (reviewer gate before any code)

**Contract:** `contract/planpilot_agent_contract_v1.8.json`

**Contract SHA-256:** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

**Feature baseline (audit-approved):** `g2-baseline-5bf299a` → `5bf299a`
**Development parent (actual G2 branch tip):** `c1e9274`
`c1e9274` is the append-only devlog successor of the baseline; it registers the
G1.0.2 approval and the deferred P2 `clock_session` defence-in-depth item. Work
starts from `c1e9274`. The tag `g2-baseline-5bf299a` is a fixed audit point —
do not move, rewrite, or checkout it.

## 1. Purpose

Make publication a contract-level transaction. Today `RuntimeAuthority.publish_plan`
(authority.py:343) enforces role, digest, lifecycle and approval semantics, but:

- it ignores the contract-registered `idempotency_key` entirely (input schema
  requires it; the web UI already sends a stable key — `p1_3.js:254`);
- `_mutate()` calls `Database._audit()` and **discards its return value**
  (authority.py:122), so the caller can never learn the real `audit_log_id`
  of this publication;
- `/publish` (api_server.py:216) never validates the **success output**
  against the contract `output_schema` (`plan_id`, `published_version`,
  `status:"PUBLISHED"`, `audit_log_id`, `additionalProperties:false`);
- a retry with the *same* key and the *same* request is handled only by the
  accidental `lifecycle.approval_set_id` equality check, and a retry with a
  *different* approval set raises a bare `ValueError` (400), not the contract
  error.

Note (reviewer correction #2, verified in code): `tools/api_server.py:140`
is in the **`/audit/status` read path**. `row["id"]` there comes from the
`audit_chain` table primary key — it is a valid audit id, not an
`authority_state` row number. The real gap is that the publish write path
cannot obtain the id of the audit row it just created, because `_mutate`
drops `_audit`'s return value. This design fixes that at the source instead
of minting ids anywhere.

G2 delivers the strict publish transaction:

```
auth → input schema → idempotency probe → replay-or-conflict →
revalidate → approval set load → lifecycle transition → real audit insert →
build response → output schema validate → persist receipt + key binding →
single commit
```

with full crash/concurrency evidence. If output validation fails, lifecycle,
audit and idempency state all roll back together.

## 2. Contract authority and exact scope

Authoritative sections:

- `tools[publish_plan]`: input_schema (5 required fields incl.
  `idempotency_key`, 16–128 chars), output_schema (4 fields, strict),
  `failure_schema = $defs.tool_error`;
- `$defs.error_details_idempotency_conflict`:
  `{idempotency_key, original_plan_id: string|null, original_status:
  string|null}`, required, `additionalProperties:false`;
- `retryability_registry`: `IDEMPOTENCY_CONFLICT: false`;
- existing codes already wired in `approval/errors.py` / `store/errors.py`:
  `PLAN_DIGEST_MISMATCH`, `PLAN_VERSION_CONFLICT`, `APPROVAL_*` family,
  `POLICY_VIOLATION` (enum includes `publish_without_confirmation`,
  `approval_scope_exceeded`);
- `tool_execution_contract` transport rules (already implemented by
  `src/planpilot/tools/middleware.py` — reused, not re-implemented).

### In scope

1. Two new persistence tables (recommended split, per reviewer):
   - `publication_receipt` — `plan_id`, `plan_version`, `plan_digest`,
     `approval_set_id`, `audit_log_id`, complete contract-valid success
     response JSON, created timestamp; UNIQUE on the binding
     `(plan_id, plan_version)` (a plan version has exactly one publication);
   - `idempotency_registry` — `tool_name`, `idempotency_key`
     (UNIQUE together), canonical request fingerprint (SHA-256 over
     `canonical()` of the exact 5-field request — reusing
     `persistence.canonical`, the same canonicalisation the audit chain uses),
     FK `→ publication_receipt`, created timestamp.
2. `PublisherService` (new module `src/planpilot/publisher.py`) implementing
   the 12-step order of §4 as ONE SQLite `BEGIN IMMEDIATE` transaction via
   `Database.transaction()`.
3. `_mutate`/publish path stops swallowing the audit return value: the
   publish flow uses the receipt's real `AUD-…` id end-to-end.
4. `IdempotencyConflictError` for the publish context: reuse the code +
   details shape (`store/errors.py` `IdempotencyConflictError` documents the
   exact contract details schema — extend or add a sibling class that takes
   an explicit `idempotency_key/original_plan_id/original_status`; do NOT
   invent a new error code; do NOT edit the contract).
5. Replay semantics (all decided in §5).
6. `/publish` route switches to middleware-mediated execution
   (`ToolErrorMiddleware.execute` with a `PreparedToolCall` — its staged
   prepare→validate-output→commit protocol is already exactly the crash
   boundary needed; api_server currently bypasses it for /publish).
7. Crash-point and concurrency test matrix (§8), evidence pack.
8. Devlog entry; baseline integrity checks (contract SHA, tag untouched).

### Out of scope

- Contract edits (frozen).
- The `clock_session` P2 trigger hardening (registered for pre-release gate;
  separate task, do not mix in).
- Decision-trace persistence beyond what `ToolExecutionOutcome` already
  exposes; scheduler; agent chat; UI redesign (the web already sends a
  stable key, no UI change required for G2; adding an Idempotency-Key header
  to UI retry logic is UI-polish, not G2).
- Generalising the registry to other tools: `tool_name` is stored NOW so the
  table is future-proof, but only `publish_plan` writes it in G2.
- Migrations of already-shipped DBs: PlanPilot DBs are disposable artifacts
  in this project; new tables are created by the `Database.__init__` executescript block
  (CREATE TABLE IF NOT EXISTS pattern, matching existing tables). If the
  reviewer later demands an in-place migration, that is a separate task.

## 3. Data model

```sql
CREATE TABLE IF NOT EXISTS publication_receipt (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id             TEXT    NOT NULL,
    plan_version        INTEGER NOT NULL,
    plan_digest         TEXT    NOT NULL,
    approval_set_id     TEXT    NOT NULL,
    audit_log_id        TEXT    NOT NULL,      -- real AUD-… from audit insert
    response_json       TEXT    NOT NULL,      -- canonical() of the exact
                                               -- contract-valid response
    created_at          TEXT    NOT NULL,      -- server clock, seconds ISO
    UNIQUE (plan_id, plan_version)             -- one publication per version
);

CREATE TABLE IF NOT EXISTS idempotency_registry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name           TEXT    NOT NULL,      -- 'publish_plan' in G2
    idempotency_key     TEXT    NOT NULL,      -- as supplied (16–128 validated)
    request_fingerprint TEXT    NOT NULL,      -- sha256(canonical(request))
    receipt_id          INTEGER NOT NULL REFERENCES publication_receipt(id),
    created_at          TEXT    NOT NULL,
    UNIQUE (tool_name, idempotency_key)
);
```

Design facts the reviewer asked to pin:

- **Fingerprint** covers ONLY the request side (5 fields verbatim; key
  included), hashed over `canonical()` bytes — same function as the audit
  chain, so "identical request" has one definition in this codebase.
- **Receipt is the single success payload.** Replay returns
  `json.loads(response_json)` unchanged — same `audit_log_id`, same status.
  The registry never stores a response itself, so key→response can never
  drift from the receipt it points at.
- **`audit_log_id` is stored text**, in the exact `AUD-%012d` form
  `_append_audit_record` already returns (persistence.py:180). The receipt is
  inserted in the SAME transaction that inserted the audit row, so the id
  cannot dangle: rollback removes both.
- **No FK from `publication_receipt` back to lifecycle rows**: PlanStore
  content/lifecycle lives serialised in `authority_state` JSON; cross-store
  integrity is enforced in the transaction's revalidation step (§4 step 5),
  not at SQL level. Stated limitation.
- Both tables are written/read ONLY inside `Database.lock`/`transaction()` —
  same discipline as `clock_session` (G1.0.2 lesson).

## 4. Transaction order — pinned

First publication executes inside ONE `db.transaction()` (BEGIN IMMEDIATE,
commit at step 12, rollback on ANY failure). Steps 1–2 run before the
transaction opens (they must not — auth and input validation cannot leave
state to roll back); the reviewer's numbered list is kept, with that
clarification:

1. **Auth role** (route: `_auth("approve_publish")` → token role `planner`
   required; 403 outside the transaction).
2. **Input schema validation** against contract
   `publish_plan.input_schema` (5 required fields, key 16–128 chars).
   Invalid → transport error, zero state touched.
3. **Open transaction; probe idempotency key**
   `SELECT … FROM idempotency_registry WHERE tool_name=? AND idempotency_key=?`.
4. **Key hit, fingerprint differs** → raise
   `IDEMPOTENCY_CONFLICT` (non-retryable) with details
   `{idempotency_key, original_plan_id, original_status}` from the original
   receipt. Transaction rolls back → zero business change. (§5 case B)
   **Key hit, fingerprint matches** → load receipt, return its stored
   response verbatim. No new audit, no revision bump. (§5 case A)
5. **Revalidate current state**: store `get_content`, `verify_digest`
   (recomputed digest == supplied digest == stored content digest, else
   `PLAN_DIGEST_MISMATCH`); `expected_plan_version` matches lifecycle
   (else `PLAN_VERSION_CONFLICT`); validator evidence present via lifecycle
   (approved/published states require it already — PlanStore invariant).
6. **Load full approval set**: `approvals.require_approved(
   approval_set_id, plan_id, version, digest, timestamp)` — expired/
   rejected/incomplete/invalidated already raise their contract errors.
7. **Lifecycle transition to PUBLISHED** via the mutation core, executing
   INSIDE the publisher's already-open transaction. Reviewer pin: the
   existing `publish_plan` → `_mutate` wrapper (authority.py:343 → 100)
   opens its OWN `db.transaction()`; the publish flow must not call that
   wrapper and must not open a second nested or sequential `BEGIN`. The
   snapshot `UPDATE authority_state … WHERE revision=?`, lifecycle
   transition, audit insert, response validation and receipt/registry
   inserts all belong to the publisher's single `BEGIN IMMEDIATE`.
   If lifecycle is already PUBLISHED for this binding, this is §5 case C,
   not a transition.
8. **Insert the real `plan_published` audit-chain record** — capture the
   return of `Database._audit(...)` / `_append_audit_record(...)`: the
   `{audit_log_id, previous_hash, event_hash}`. Never mint separately.
9. **Build the success response** from steps 7–8: `{plan_id,
   published_version, status:"PUBLISHED", audit_log_id}` — the real id.
10. **Validate output schema** (`publish_plan.output_schema`, strict) and
    canonicalise the response to `response_json`. Failure here is an
    internal defect → `INTERNAL_ERROR`, transaction rolls back (steps
    7–9 never commit).
11. **Persist receipt + registry row**: INSERT `publication_receipt`
    (binding + audit id + canonical response), INSERT
    `idempotency_registry` (key + fingerprint → receipt).
12. **COMMIT.** One shot. Middleware then serialises the validated output;
    nothing after commit can alter persisted state.

The invariant this pins: **lifecycle transition, audit insert, response
validation, receipt and key binding share one transaction and one clock**.
There is no commit point between them, so every crash in §8 lands either on
"nothing happened" or "the whole publication happened".

Concurrency note: BEGIN IMMEDIATE serialises writers; two same-key callers
either both replay (same key + same payload is case A/B) or one sees the
other's committed registry row (case B/C). The authority's optimistic
`revision` UPDATE (inside `authority_state`) remains the second guard and
must not be weakened.

## 5. Idempotency semantics — decision table

Notation: request = the 5 input fields. `key` = idempotency_key.

| # | Situation | Result | Business change | Audit |
|---|---|---|---|---|
| A | same key, same fingerprint | stored response returned verbatim (same `audit_log_id`, same `published_version`) | none | none |
| B | same key, different fingerprint | `IDEMPOTENCY_CONFLICT` (non-retryable) | none | none |
| C | new key, binding already published: same plan_id+version+digest **and same approval_set_id** | **alias**: return the stored receipt response verbatim, insert a registry row for the new key → same receipt | alias row only | none |
| D | new key, already-published binding with a DIFFERENT approval_set_id | fail closed: `IDEMPOTENCY_CONFLICT`, `original_status:"PUBLISHED"` (a version publishes exactly once with exactly one approval set; a second set must go through a new version) | none | none |
| E | first publication | full 12-step transaction | lifecycle + audit + receipt + registry | 1× `plan_published` |
| F | any precondition failure (role, schema, version, digest, approval, window) | existing contract errors transport unchanged | none | none |

Reviewer decision embedded in C: alias IS registered (new key → existing
receipt). Rationale: after the client crashes between commit and response
(§8 case 5) it must be able to recover with a fresh key; returning case-D
conflict instead would leave a published plan permanently unrecoverable for
a client that lost the receipt. The alias path is deterministic replay of
the stored receipt, never a second `plan_published` audit, so the audit
chain keeps the one-publication-per-version invariant.

Exact retry returns identical `audit_log_id` — required by the reviewer's
test matrix and satisfied structurally by storing the response in the
receipt, not reconstructing it.

## 6. Module placement

- `src/planpilot/publisher.py` — `PublisherService(db, authority)`:
  owns the 12-step transaction and the table queries. `RuntimeAuthority`
  keeps lifecycle/approval semantics; the publisher orchestrates the one
  write path it does not currently reach: idempotency + receipt + real audit
  id. Alternative (rejected): putting it on `RuntimeAuthority` directly —
  authority is a JSON-snapshot core, publisher is a SQLite-transaction core;
  the reviewer asked for design clarity, not a god object.
- `tools/api_server.py` `/publish` becomes
  `middleware.execute("publish_plan", body, PublisherPreparedCall(...))` —
  `PreparedToolCall.prepare` runs steps 3–11 inside the open transaction and
  returns the candidate response; middleware validates the output (§4 step
  10 equivalent, defense in depth); `commit()` is the step-12 COMMIT;
  `rollback()` releases the transaction. This reuses the already-tested
  prepare→validate→commit→rollback guard rather than inventing a second
  staging protocol.
- Error classes: `IDEMPOTENCY_CONFLICT` raised as a `StoreError` sibling in
  `store/errors.py` shape (constructor takes explicit key/original bindings).
  The publish-context class lives next to `PublisherService`; its details
  schema and code string are imported from the existing store error family
  so transport behaviour is one mechanism.
- Tests: `tests/unit/test_publisher_transaction.py` (+ crash-matrix helpers).

## 7. Compatibility

- Contract untouched; SHA `b92e53f4…` re-verified in Phase 0 of `tasks.md`.
- Existing `publish_plan` behavior preserved: non-planner role, digest
  mismatch, version conflict, approval-window errors keep their exact codes.
  The bare `ValueError("publication retry uses a different approval set")`
  disappears — case D replaces it with the contract error (400 → 409 is the
  only externally visible correction; it was already an uncontracted hole).
- `_Unchanged` short-circuit in `_mutate` remains for other writers; the
  publish flow no longer depends on it for idempotency.
- `bind_clock`/clock_session: untouched (registered P2 is deferred).
- Web UI: already sends a stable idempotency key; response shape gains the
  `audit_log_id` the output schema already required. No JS change required
  in G2 (reviewer may opt out of any UI polish).

## 8. Crash & concurrency test matrix (blocking evidence)

Crash points simulated by fault injection at exact steps (monkeypatched
raising hook in the publisher, or direct SQL kill-equivalent: raise after
each write):

1. **crash before lifecycle update** (between 6 and 7) → nothing persisted;
   replay is a fresh first publication; audit count unchanged.
2. **crash after lifecycle, before audit insert** (between 7 and 8) →
   lifecycle NOT durable (same txn); replay fresh; authority_state revision
   unchanged; zero new audit rows.
3. **crash after audit, before receipt insert** (between 8 and 11) → audit
   row NOT committed (rollback removes it — proves audit and txn are one
   unit, `verify_audit()` still passes after restart).
4. **crash after receipt, before COMMIT** (between 11 and 12) → identical to
   3 on restart: full replay yields a fresh first publication (case E).
5. **crash after COMMIT, before HTTP response** → restart; client retries
   with same key (case A) → byte-identical stored response, same
   `audit_log_id`, NO second `plan_published` row; retry with a NEW key
   (case C) → same receipt via alias, still ONE audit row.
6. **two connections, same key** (concurrent, barrier-synced) → both get the
   identical response; registry has exactly one row; one audit row; one
   revision bump.
7. **two connections, same key, different payloads** → one E, one B
   (conflict) with correct original bindings in details; order-insensitive
   (whoever commits first wins; loser conflicts regardless).
8. **restart replay after commit** → covered by 5 (genuine process restart
   with a second `Database` over the same file — G1.0.2 discipline: reopen,
   not re-anchor; reuse `clock_session` persisted session).
9. **output schema mutated before validation** (fault-injected bad
   `status`/extra field) → txn fully rolls back: lifecycle not PUBLISHED,
   no audit row, no receipt, no registry row; response is `INTERNAL_ERROR`.
10. **exact retry audit identity** → case A returns same `audit_log_id` and
    `audit_chain_head.entry_count` unchanged.
11. **retry adds no `plan_published`** → count query before/after case A/C.
12. **negative matrix, all zero-change**: non-planner, expired approval,
    rejected, invalidated set, stale expected_plan_version, digest
    mismatch, approval-window-closed clock → existing contract errors,
    registry/receipt/audit/lifecycle untouched.

Evidence pack: `tests/evidence/g2-publisher-transaction/` with raw logs +
`EVIDENCE.json` following the G1.0.2 rule — hashes computed from final
committed (HEAD, eol=lf) blobs, and the `test_evidence_integrity.py` self
check must pass inside the harness copy too.

## 9. Verification gates

Phase order in `tasks.md`; every phase ends green before the next starts:
targeted publisher tests → clock/evidence regressions → full unit → full
suite → negctl + restore hash → contract SHA → `git diff --check` → devlog
entry → evidence pack with verified hashes.

## 10. Explicit non-goals for G2

- No changes to the contract file, `schema_distribution`, or error registry.
- No `requirements.md` in this directory.
- No merge into `tool-error-middleware` spec (reuses it as a component).
- No publisher for tools other than `publish_plan`.
- No `clock_session` triggers (deferred P2, registered in devlog).
- No HTTP `Idempotency-Key` header transport (key stays in the validated
  contract body; header mapping is a transport decision for a later part).
