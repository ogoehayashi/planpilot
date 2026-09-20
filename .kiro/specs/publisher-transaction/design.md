# Design — `publisher-transaction`

**Mode:** Design-First (reviewer gate before any code)

**Contract:** `contract/planpilot_agent_contract_v1.8.json`

**Contract SHA-256:** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`

**Feature baseline (audit-approved):** annotated tag `g2-baseline-5bf299a` →
`5bf299a`. Fixed audit point — do not move, rewrite, or check it out.
**G2 lineage parent:** `c1e9274` (append-only devlog successor of the
baseline; registers the G1.0.2 approval and the deferred P2 `clock_session`
defence-in-depth item). **Phase 0 invariant (reviewer round-2):** HEAD must be
the reviewer-approved publisher-spec commit — the commit containing these
four files — and `c1e9274` must be an ancestor of it; implementation starts
from that tip, never from a `c1e9274` checkout (that would drop this spec).

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
audit and idempotency state all roll back together.

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
   the 12-step order of §4 as ONE SQLite transaction held OPEN across the
   middleware's prepare → output-validation → commit stages by the explicit
   state machine in §4.5. Note (reviewer P0): `Database.transaction()`
   (persistence.py:155) is a context manager that COMMITs on `with`-exit —
   it CANNOT span those stages, so the publisher owns a manual
   `BEGIN IMMEDIATE` under `Database.lock` instead.
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
    audit_log_id        TEXT    NOT NULL UNIQUE, -- real AUD-… from audit
                                               -- insert; UNIQUE per
                                               -- reviewer round-2: a
                                               -- mis-built receipt can
                                               -- never share an audit id
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

The HTTP layer runs exactly this order (reviewer P1 transport pin):

```
HTTP JSON decode
→ authentication          (route: _auth("approve_publish"), 403 on failure)
→ middleware.execute("publish_plan", body, PublisherPreparedCall(...))
    → middleware input-schema validation   (THE ONLY input validation point)
    → prepare / output-validation / commit  per §4.5
→ write outcome.wire_bytes to the socket
```

The route-level `validate_tool_payload(body, "publish_plan", …)` that
`/publish` performs today (api_server.py:218) is **removed**: the
middleware already validates the input schema inside `execute`
(middleware.py:177), and a second pre-validation outside it sends invalid
input through a bare 400 instead of the contract `tool_error` envelope.
Schema validation has exactly one entry point. Auth stays outside the
middleware (role/403 is a transport concern, not a tool execution — the
contract keeps it out of `tool_error`). Reviewer P1 note, verified:
today `/publish` calls `authority.publish_plan` **directly** — no
middleware at all — so the rewiring in §4.1/§6 is a real behavior change
(bare 400/409 ad-hoc envelopes → contract `tool_error` transport), not a
refactor in place.

First publication then executes inside ONE transaction (manual
BEGIN IMMEDIATE per §4.5, commit at step 12, rollback on ANY failure).
Steps 1–2 run before the transaction opens (they must — auth and input
validation cannot leave state to roll back); the reviewer's numbered list
is kept with that clarification:

1. **Auth role** (route: `_auth("approve_publish")` → token role `planner`
   required; 403 outside the transaction, outside the middleware).
2. **Input schema validation** INSIDE `middleware.execute`, against contract
   `publish_plan.input_schema` (5 required fields, key 16–128 chars).
   Invalid → contract `tool_error` envelope (`INVALID_INPUT`) in
   `outcome.wire_bytes`, HTTP 400, zero state touched.
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
   (else `PLAN_VERSION_CONFLICT`). **Validator evidence is re-checked
   EXPLICITLY, never inferred from lifecycle** (reviewer P0): lifecycle
   holds only state + approval binding, while evidence lives in
   `ApprovalService._validated[(plan_id, version, digest)]`
   (approval/service.py:96). The publisher calls a new read-only method
   `approvals.require_validated_binding(plan_id, version, digest)` which
   fails closed (zero change; `VALIDATION_FAILED` when the record is
   missing/fails its checks, non-retryable — registered in
   `retryability_registry`, no new code invented) unless ALL of:
   - a validation record exists for exactly that binding;
   - `digest_verified is True`;
   - `is_feasible is True`;
   - `hard_violations == []`;
   - `recomputed_plan_digest == plan_digest`;
   - the approval set presented in step 6 was derived from the COMPLETE
     required-action set of THAT validation record (binding equality of
     plan_id/version/digest already enforced by `require_approved`).
   Negative test (reviewer-named): keep the APPROVED approval set, delete
   the validation record → publish must fail with zero change.
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
either both replay (same key + same payload is case A) or the loser sees
the winner's committed registry row and gets case B (different payload) or
— for a *new* key against an already-published binding — the case C/D
decision. The authority's optimistic `revision` UPDATE inside
`authority_state` remains the second guard and must not be weakened (§4.5
reloads it inside the open transaction).

## 4.5 `PublisherPreparedCall` — cross-stage transaction state machine (P0)

Reviewer P0, verified against code: `Database.transaction()`
(persistence.py:155) is a context manager that COMMITs on `with`-exit —
it **cannot** hold a transaction open across
`prepare → middleware output-validation → commit`. `PublisherPreparedCall`
therefore owns the transaction manually under `Database.lock` (the
`threading.RLock` at persistence.py:58, the same lock
`transaction()` takes), never entering the context manager:

```
INIT
  prepare():
      db.lock.acquire()                     # _holds_lock = True
      conn.execute("BEGIN IMMEDIATE")       # set _txn_open ONLY on success
        # BEGIN failure: no transaction exists; the exception path calls
        # rollback(), which must see _txn_open False and therefore do
        # nothing but release the lock exactly once (no conn.rollback()
        # against a non-transaction, no double release).
      reload authority snapshot INSIDE the open transaction:
        SELECT revision, plan_store_json, approval_service_json
          FROM authority_state WHERE singleton=1
        → staged clones (store, approvals) + staged revision, built with
          the SAME transaction-less primitives _mutate uses (_clone,
          _load_pair); the publisher NEVER calls _mutate() and never
          opens a second BEGIN.
      run §4 steps 3–11 against the staged copies (case A/C/D/B decisions
        resolve here; failure → exception → rollback)
      KEEP transaction and lock open; state: INIT → PREPARED
      return the candidate response to the middleware
  middleware output-schema validation (defense-in-depth over step 10)
  commit():
      require state == PREPARED (any other state = protocol defect →
        INTERNAL_ERROR)
      conn.commit()                         # the durable point; _txn_open=False
      state: PREPARED → COMMITTED
      synchronise RuntimeAuthority in-memory store/approvals/revision
        from the staged copies (exactly the field swap _mutate performs
        today at authority.py:123, just relocated after conn.commit()
        while the lock is still held)
      release lock exactly once (finally); state → FINISHED
  rollback():
      IDEMPOTENT by contract: legal from every state; state set with
        compare-and-set so a second call is a silent no-op (the
        middleware's _RollbackGuard may already have called it).
      if _txn_open: conn.rollback() while the lock is still held
      discard staged copies
      release lock exactly once; state → ROLLED_BACK
```

Pinned rules (each reviewer round-2, all blocking):

1. **rollback() idempotent** — one CAS-protected transition out of
   PREPARED; `_RollbackGuard` (middleware.py:73–89) calling `rollback()`
   after the prepare exception already unwound, or after a failed commit,
   must not double-release the RLock (the RLock *is* reentrant per
   thread, but the design pins release-once semantics regardless:
   `_holds_lock` CAS to False is what gates the `release()` call).
2. **BEGIN failure never releases what was never acquired** — lock
   acquire precedes BEGIN, so the BEGIN-failure path releases the lock and
   nothing else; no `conn.rollback()` against a connection without an
   open transaction.
3. **Commit-then-memory-sync gap is fail-closed**: if anything raises
   between `conn.commit()` and the in-memory swap (including the swap
   itself), the database is durable while `RuntimeAuthority`'s snapshot is
   stale — serving further writes from it is exactly the "SQLite
   committed / memory not committed" split the reviewer named. The call
   marks the authority **poisoned**: a `_poisoned` flag set on
   `RuntimeAuthority`, checked at the top of every read/write entry
   point, raising `INTERNAL_ERROR` ("authority snapshot diverged; reload
   required") until `authority.reload_from_db()` re-reads
   `authority_state` under `db.lock` and clears it. The poisoned-authority
   path gets its own test (§8 case 13).
4. **One staging core** — the publish flow extracts and reuses the
   existing transaction-less primitives: `RuntimeAuthority._clone()`,
   `_serialize()` and the guarded snapshot UPDATE
   (`authority_state … WHERE singleton=1 AND revision=?`, rowcount guard,
   authority.py:114–121) as a `_mutate`-shared helper taking an **already
   open connection**. `_mutate()` keeps calling it inside its own
   `transaction()`; the publisher calls it inside the §4.5 manual
   transaction. No second state machine, no copied optimistic-revision
   logic.

## 5. Idempotency semantics — decision table

Notation: request = the 5 input fields. `key` = idempotency_key.

| # | Situation | Result | Business change | Audit |
|---|---|---|---|---|
| A | same key, same fingerprint | stored response returned verbatim (same `audit_log_id`, same `published_version`) | none | none |
| B | same key, different fingerprint | `IDEMPOTENCY_CONFLICT` (non-retryable) | none | none |
| C | new key, binding already published: same plan_id+version+digest **and same approval_set_id** | **alias** (reviewer-approved): return the stored receipt response verbatim, insert a registry row for the new key → same receipt. The alias row may NOT mutate the receipt, its `audit_log_id`, or the original registry row (asserted in §8) | alias row only | none |
| D | new key, already-published binding with a DIFFERENT approval_set_id | fail closed: **`POLICY_VIOLATION`** (reviewer correction — this is NOT a key-reuse conflict; the key is new), details `{violated_policy:"approval_scope_exceeded", blocked_action:"publish_plan", approval_action:"publish_plan", security_event_id:null}` — all four fields required by `$defs.error_details_policy_violation`, `additionalProperties:false`, non-retryable (a version publishes exactly once with exactly one approval set; a second set must go through a new version) | none | none |
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

### 6.1 Middleware wiring (server side) — today there is none

Verified: `api_server.py` never constructs `ToolErrorMiddleware`; the
middleware currently only runs in tests. G2 creates it once per server in
`Server.__init__` (api_server.py:55–60 area):

```
decision_writer = DecisionTraceWriter(AuditTrail(db))   # audit.py:133,
                                                        # ctor takes AuditTrail
self.middleware = ToolErrorMiddleware(observer=decision_writer)
```

`ToolErrorMiddleware.__init__` is keyword-only
(`uuid_factory`, `clock`, `observer`; middleware.py:94–103) — there is no
`authority` or `trace_writer` parameter, and no per-server singleton is
required beyond `db`/`authority` closure in the route. The `observer`
callback contract: `_finish` (middleware.py:116–152) builds the bounded
event dict (tool_name, correlation_id, is_error, error_code, retryable,
elapsed_seconds, payload_bytes, committed, entity_references,
trace_metadata) and calls `self._observer(event)` with exceptions swallowed
(observability must never change outcomes). `DecisionTraceWriter.__call__`
(audit.py:145) is exactly that observer: it sequences per correlation_id and
appends a `decision_trace` record via `AuditTrail.append_trace`. Wiring it
gives every publish invocation (first, replay, conflict, failure) one trace
row — §8 case 18 asserts the accounting (traces may grow on replay;
`plan_published` audit events must not).

### 6.2 Route order and the single schema-validation entry

`/publish` becomes:

```
HTTP JSON decode (do_POST)
→ authentication                (route _auth("approve_publish"); 403)
→ middleware.execute("publish_plan", body, PublisherPreparedCall(...))
   → execute validates input ONCE via validate_tool_payload(...,
     "input_schema", "publish_plan_input") (middleware.py:177) and turns
     any failure into the contract tool_error envelope
     (validated_failure, errors.py:83)
   → prepared.prepare: §4 steps 3–11, transaction held open (§4.5)
   → execute validates output via validate_tool_payload(...,
     "output_schema", ...) + json_wire (middleware.py:210–211)
   → prepared.commit (middleware.py:216) = step-12 COMMIT
→ write outcome.wire_bytes verbatim with the HTTP status chosen below
```

The route-level `validate_tool_payload(body, "publish_plan", …)` that
`/publish` performs today (api_server.py:218–220) is **removed**: the
middleware already validates the input schema inside `execute`
(middleware.py:177). Duplicating it outside would route invalid input into
the bare `except (ValueError, KeyError) → 400` path (api_server.py:169–170)
instead of the contracted `tool_error` envelope — the reviewer's exact
concern. Authentication stays in the route (there is no HTTP concept inside
the middleware); nothing else may pre-validate.

### 6.3 `wire_bytes` → HTTP, and the status mapping

`ToolExecutionOutcome` fields are exactly `{tool_name, correlation_id,
is_error, payload, wire_bytes}` (middleware.py:64–72) — **no status_code**.
The contract defines no HTTP statuses either (its `failure_transport` only
mandates the tool_error envelope). So G2 adds one route-local mapping in
`/publish` that reproduces today's classifications (api_server.py:163–173:
PermissionError→403, Store/ApprovalError→409, ValueError→400, other→503)
keyed off `outcome.payload["error_code"]` when `is_error`:

| outcome | status |
|---|---|
| success | 200, write `outcome.wire_bytes` |
| `INVALID_INPUT` (input-schema failure inside `execute`) | 400 |
| `VALIDATION_FAILED` | 400 |
| `POLICY_VIOLATION` | 403 |
| `IDEMPOTENCY_CONFLICT`, `PLAN_VERSION_CONFLICT`, `PLAN_DIGEST_MISMATCH`, `INVALID_STATE_TRANSITION`, approval-domain codes | 409 |
| `INTERNAL_ERROR`, `DEADLINE_EXCEEDED`, any unmapped code | 503 |

`Content-Type: application/json; charset=utf-8`, `Content-Length =
len(wire_bytes)`, bytes written unmodified (no re-serialisation — that is
the middleware's `json_wire` guarantee). This table is a route convention
continuation, not a contract claim; reviewers may move it into the
transport appendix of a later contract version.

### 6.4 Errors and tests

- `IDEMPOTENCY_CONFLICT` reuses the real existing class
  `IdempotencyConflictError` (store/errors.py:270, `code =
  "IDEMPOTENCY_CONFLICT"`, non-retryable) — no new error class for case B.
  Case D uses a contract-shaped `POLICY_VIOLATION` error with
  `violated_policy: "approval_scope_exceeded"`; no such class exists in the
  store/approval error modules today, so the publisher module defines it
  once — a `StoreError` sibling carrying `code = "POLICY_VIOLATION"` (same
  pattern as `store/errors.py:278`), passed through the same
  `validated_failure` envelope mechanism (errors.py:83) so transport
  behaviour stays one path.
- Tests: `tests/unit/test_publisher_transaction.py` (+ crash-matrix helpers,
  incl. subprocess hard-kill helpers for §8 cases 14–15).

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
each write). Exception-rollback alone only proves code paths, not SQLite/WAL
recovery under process death — so cases 14–15 additionally **hard-kill a
real subprocess** (`os._exit`) at two representative points and verify from
a fresh process over the same DB file (reviewer P1):

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
13. **poisoned-authority fail-closed** (§4.5 rule 3): fault-inject an
    exception between `conn.commit()` and the in-memory swap → the
    authority is poisoned; every read/write entry point raises
    `INTERNAL_ERROR` until `authority.reload_from_db()` (new method:
    re-reads the `authority_state` singleton under `Database.lock`,
    rebuilds store/approvals/revision, clears the flag). After reload the
    memory snapshot equals the DB (revision bumped, lifecycle PUBLISHED) —
    the split state is never served.
14. **hard-kill subprocess: audit written, receipt not** (WAL proof):
    real subprocess (`sys.executable` script driving one first publication)
    `os._exit()`s from the fault hook between step 8 and step 11; parent
    opens the same DB in a fresh process → zero new `plan_published` audit
    events, lifecycle not PUBLISHED, receipt/registry empty,
    `verify_audit()` passes, `clock_session` high-water intact.
15. **hard-kill subprocess: COMMIT done, HTTP response not sent**:
    subprocess commits the full publication (step 12) then `os._exit()`s
    before the route writes bytes; fresh process replays the same key →
    byte-identical stored response, same `audit_log_id`, exactly ONE
    `plan_published` (case 5 re-proven under a real kill).
16. **alias immutability** (§5 case C): snapshot the receipt row, its
    `audit_log_id` and the original registry row; after an alias replay
    they are byte-identical; the registry gains only the new-key row;
    `audit_chain_head.entry_count` unchanged (reviewer: alias may not
    mutate receipt/audit id/original row).
17. **validator-evidence negative** (§4 step 5, reviewer-named): keep the
    APPROVED approval set, delete the validation record from the approval
    snapshot → publish fails `VALIDATION_FAILED` with zero change in
    registry/receipt/audit/lifecycle.
18. **trace observer accounting**: first publication, case-A replay and
    case-B conflict each produce exactly one `DecisionTraceWriter`
    tool-invocation trace (middleware `_finish` fires for every outcome);
    replays may add traces but never a second `plan_published` event.

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
