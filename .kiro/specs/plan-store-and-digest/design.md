# Design — plan-store-and-digest

**Spec type:** Design-First (requirements are fixed by `contract/planpilot_agent_contract_v1.8.json`)
**Contract sha256:** `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`
**Tracks:** `plan_store`, `$defs.plan`, `$defs.plan_content`, `$defs.plan_lifecycle`,
`scheduling_engine.determinism.canonical_serialization`

---

## 1. What the contract requires

`plan_store.purpose`:

> Server-side store of record for immutable plan content and mutable lifecycle
> state. The Agent never receives a full plan aggregate; it receives bounded
> `plan_option_summary` records and reads details through `validate_plan` or the
> UI data path.

`plan_store.records`:

| record | mutability |
|---|---|
| `plan_content` | **immutable**, content-addressed by `plan_digest`, written once per version |
| `plan_lifecycle` | **mutable** status, approval-set binding, published version, `updated_at` |

`plan_store.versioning`:

> `plan_version` increments on every regeneration for the same horizon; a
> superseded version moves to `lifecycle.status=SUPERSEDED` and its approval sets
> are invalidated.

`plan_store.retention`:

> Every version and its audit trail is retained for the hackathon evidence pack;
> **digests are recomputable from stored content at any time.**

---

## 2. The digest rule (the whole point of this spec)

From `$defs.plan_content.properties.plan_digest.description`:

> SHA-256 of the canonical immutable content **excluding only** `plan_digest` and
> `engine.canonical_plan_hash`; **equals** `engine.canonical_plan_hash`

From `scheduling_engine.determinism.canonical_serialization`:

> Sort operations by `start_time`, `machine_id`, `order_id`, `lot_no` and
> `operation_no`; serialize datetimes as RFC 3339 with `+08:00`. Hash only the
> immutable `plan_content` object, excluding `plan_content.plan_digest` and
> `plan_content.engine.canonical_plan_hash` **to avoid circularity**. Lifecycle
> and observed runtime fields are separate records and never enter the digest.

### 2.1 Canonical form — fixed, not negotiable

Any of these choices left implicit would make the digest unreproducible on
another machine, so all are pinned here and asserted in tests:

| decision | value | why |
|---|---|---|
| encoding | UTF-8 | cross-platform |
| key order | `sort_keys=True` | dict insertion order must not affect the hash |
| separators | `(",", ":")` | no whitespace ambiguity |
| `ensure_ascii` | **`False`** | non-ASCII (Chinese product notes) hashes as itself; `True` would silently double the byte count of any non-ASCII string |
| float repr | shortest round-trip (`repr`) | Python's default; stable across runs on the same version |
| non-finite floats | **rejected** | `NaN`/`Infinity` are not valid JSON and `json.dumps` emits them by default — a silent reproducibility hole |
| signed zero | **normalize `-0.0` to `0.0`** | IEEE-754 signed zeros compare equal and have identical planning meaning; they must not create different digests |
| isolated UTF-16 surrogates | **rejected as `CanonicalizationError`** | `ensure_ascii=False` preserves them until UTF-8 encoding, where a raw `UnicodeEncodeError` would otherwise escape the registered error model |
| int vs float | type-preserving | `1` and `1.0` serialize differently, which is correct: the contract distinguishes `integer` from `number` in `kpis` |
| excluded fields | `plan_digest`, `engine.canonical_plan_hash` | required to avoid circularity |
| operations order | `(start_time, machine_id, order_id, lot_no, operation_no)` | the contract's sort key |

### 2.2 Two identities that must both hold

```
canonical_plan_digest(content) == content["engine"]["canonical_plan_hash"]
canonical_plan_digest(content) == content["plan_digest"]
```

A stored plan whose digest does not recompute to the same value is
`PLAN_DIGEST_MISMATCH` — the contract's defence against a tampered or corrupted
plan reaching approval or publish. **This is also the defence against the known
gateway failure mode** (starter-kit `proxy.py`: malformed tool-call XML once made
Hermes *fabricate tool results*). A fabricated plan cannot produce a matching
digest.

### 2.3 Lifecycle never enters the digest

`plan_lifecycle` carries `status`, `approval_set_id`, `published_version`,
`updated_at` — all mutable. Including them would mean every status transition
changed the digest and invalidated approvals bound to it. Tests assert that
mutating any lifecycle field leaves the digest unchanged.

---

## 3. Module design

```
src/planpilot/store/
├── digest.py        canonical serialization + SHA-256; pure functions, no I/O
├── errors.py        store exceptions carrying contract-shaped error details
└── plan_store.py    PlanStore: content (immutable) + lifecycle (mutable)
```

Dependency rule (from `.kiro/steering/structure.md`): nothing under `store/`
imports from `inference/`. The digest is computed without an LLM in the loop.

### 3.1 `digest.py`

```python
canonical_json(obj) -> str          # pinned form; controlled error on unencodable/non-finite input
canonical_plan_digest(content) -> str
sort_operations(operations) -> list # contract sort key, stable
assert_digest_consistent(content)   # the two identities in §2.2
```

`canonical_plan_digest` is a **pure function of its argument**. It must not read
a clock, a file, or an environment variable — otherwise the same content would
hash differently on two machines and the retention promise
("digests are recomputable from stored content at any time") would be false.
Its supported domain includes malformed operation sort-key types so ordering can
be computed deterministically. Inputs that have no valid UTF-8 JSON encoding
(isolated surrogates) are rejected with `CanonicalizationError`; "pure" does not
mean every Python object must be accepted.

### 3.2 `errors.py`

Exceptions, not return codes. Each carries `code` and `details` already shaped to
the contract's `error_details_*` schema, so the tool layer can build a
`tool_error` without re-deriving anything:

| exception | `error_code` | details schema (required fields) |
|---|---|---|
| `DigestMismatchError` | `PLAN_DIGEST_MISMATCH` | `expected_plan_digest`, `recomputed_plan_digest` |
| `VersionConflictError` | `PLAN_VERSION_CONFLICT` | `expected_plan_version`, `actual_plan_version` |
| `PlanNotFoundError` | `STATE_NOT_FOUND` | `lookup_kind`, `requested_state_id`, `requested_plan_id` |

All three are non-retryable per `tool_execution_contract.retryability_registry`.

### 3.3 `plan_store.py`

```python
PlanStore()
  .put_content(content) -> str           # first version only; verifies schema + digest
  .commit_new_version(content, ts) -> dict # contiguous, atomic supersede + outbox
  .get_content(plan_id, version=None) -> dict
  .get_lifecycle(plan_id, version=None) -> dict
  .create_lifecycle(plan_id, version, digest, ts) -> dict # always DRAFT
  .transition(plan_id, version, new_status, ts, approval_set_id=None,
              published_version=None, expected_plan_version=None) -> dict
  .supersede(plan_id, version, ts) -> None
  .pending_superseded() -> list[dict]    # non-destructive, at-least-once delivery
  .acknowledge_superseded(..., ts) -> dict # after idempotent consumer commits
  .latest_version(plan_id) -> int
  .verify_digest(plan_id, version) -> str # recompute from stored bytes
  .dump_state() / .load_state()           # JSON persistence for the evidence pack
```

**Invariants enforced on every mutation:**

1. `put_content` is **write-once**. Rewriting the same `(plan_id, plan_version)`
   with different bytes raises. Rewriting with identical bytes is idempotent and
   allowed (a retry must not fail).
2. `put_content` **recomputes** the digest and rejects a mismatch before storing —
   a corrupt plan never enters the store.
3. Every stored `plan_content` validates against `$defs/plan_content` with
   `additionalProperties: false`, so an unknown field cannot ride along.
4. `supersede` sets `status=SUPERSEDED` and appends one immutable history event.
   Delivery state is separate: the approval service reads pending events,
   invalidates idempotently, then acknowledges. A crash before acknowledgement
   retries rather than loses the invalidation. **No silent cross-module mutation
   and no destructive drain.**
5. Persistence writes canonical JSON with LF newlines, so a dumped store is
   byte-reproducible (same rule as `.gitattributes`).

**Clock is injected, never read.** Every mutating method takes `ts`. A store that
calls `datetime.now()` internally cannot be tested deterministically and would
make the evidence pack unreproducible.

### 3.4 Status transitions allowed by this module

`$defs.plan_lifecycle.status` enum: `DRAFT`, `PROPOSED`, `AWAITING_APPROVAL`,
`APPROVED`, `PUBLISHED`, `BLOCKED`, `SUPERSEDED`.

The **authoritative** transition table is `workflow.transitions` (11 states,
lifecycle status is a projection of workflow state). This module does not copy
the complete graph. It does enforce lifecycle-local facts that can never be valid
under any orchestration: approval-bearing states are active-only, APPROVED needs
an existing approval-set binding, PUBLISHED follows APPROVED with
`published_version == plan_version`, and approval bindings cannot be replaced.
SUPERSEDED is terminal. Public `transition()` cannot target SUPERSEDED because
that would omit its required invalidation event. A later replanning cycle retires
the old PUBLISHED version only through the atomic `supersede()` /
`commit_new_version()` route.

---

## 4. What this spec does NOT do

- No LLM calls. No Bedrock. No network.
- No dataset access — fixtures are constructed inline from the contract schemas.
- No hard-constraint checking (that is `validation/hard_constraints.py`).
- No approval sets (that is `approval/service.py`); this module only records that
  a supersede happened so the approval service can invalidate.
- No workflow state machine (`workflow/state_machine.py`).

---

## 5. Test strategy

Runtime evidence, not static assertion. Every test runs without network or
credentials.

| test | proves |
|---|---|
| digest stable across 200 runs and dict-key shuffles | §2.1 canonical form |
| digest stable after `random.shuffle` of `operations` | the contract sort key actually decides order |
| digest changes when any single content field changes | no field is silently ignored |
| digest unchanged when any lifecycle field changes | §2.3 |
| `NaN` / `Infinity` rejected | §2.1 non-finite guard |
| isolated surrogate in a value or key becomes contract-shaped error | §2.1 UTF-8 domain |
| `-0.0` and `0.0` share a digest without input mutation | §2.1 signed-zero normalization |
| non-ASCII string digests identically to its UTF-8 bytes | `ensure_ascii=False` |
| both identities in §2.2 hold | digest == `canonical_plan_hash` == `plan_digest` |
| write-once: different bytes rejected, identical bytes idempotent | invariant 1 |
| corrupt plan rejected at `put_content`, not at read | invariant 2 |
| unknown field rejected | invariant 3 |
| `supersede` sets status and records the event without touching approvals | invariant 4 |
| unacknowledged event survives dump/load and is retried | durable outbox; no crash-window loss |
| acknowledged event leaves pending delivery but remains in history | retention + idempotent delivery |
| stale approval request and direct DRAFT→PUBLISHED are rejected | lifecycle-local authority boundary |
| `dump_state()` → `load_state()` → re-digest identical | retention promise |
| every raised error's `details` validates against its contract schema | §3.2 |
| injected clock: no `datetime.now()` reachable from store code | §3.3 |

Plus a **negative control**: each guard above is proven to actually fail when the
defect is injected. A test suite that cannot fail is not a test suite — this
project has already shipped one assertion containing `or True`.

---

## 6. Known risks

| risk | mitigation |
|---|---|
| Digest algorithm drift between this module and a future TypeScript UI | canonical form is documented in §2.1 and pinned by `test_digest_canonical_form`; any second implementation must match the fixture hashes committed here |
| Float repr differs across Python versions | pin Python 3.11 in `requirements.txt`; the evidence pack records the interpreter version |
| Store grows unbounded during a demo | `retention` requires keeping every version, so this is by design; note the bound in the demo script rather than adding eviction |
