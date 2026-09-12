# Tasks — plan-store-and-digest

**Design:** `./design.md` · **Contract:** `contract/planpilot_agent_contract_v1.8.json`
**Workflow:** Design-First. Requirements are the contract; do not re-derive them.

Each task names the contract clause it implements, so a reviewer can map any diff
back to the clause that requires it. Tasks are ordered by dependency; Kiro may run
independent tasks concurrently (see `.kiro/specs/README.md`).

---

## Phase 1 — canonical digest (no dependencies)

- [x] **1.1 Implement `src/planpilot/store/digest.py`**
      `canonical_json`, `sort_operations`, `canonical_plan_digest`,
      `assert_digest_consistent`.
      Pins every decision in design.md §2.1: UTF-8, `sort_keys=True`,
      separators `(",", ":")`, `ensure_ascii=False`, reject non-finite floats,
      exclude `plan_digest` and `engine.canonical_plan_hash`.
      Sort key: `(start_time, machine_id, order_id, lot_no, operation_no)`
      per `scheduling_engine.determinism.canonical_serialization`.
      **Pure functions only — no clock, no file, no env read.**

- [x] **1.2 Test determinism of the canonical form**
      `tests/unit/test_digest_determinism.py`
      200 repeat runs identical · dict-key insertion order shuffled → identical ·
      `random.shuffle(operations)` across 10 seeds → identical ·
      `NaN` and `Infinity` rejected · non-ASCII hashes as its UTF-8 bytes ·
      `1` vs `1.0` produce different digests.

- [x] **1.3 Test the two digest identities**
      `tests/unit/test_digest_identity.py`
      `canonical_plan_digest(content) == content["plan_digest"]` and
      `== content["engine"]["canonical_plan_hash"]`.
      Changing any single content field changes the digest.
      Changing any lifecycle field does **not**.

## Phase 2 — errors shaped to the contract (no dependencies)

- [x] **2.1 Implement `src/planpilot/store/errors.py`**
      `DigestMismatchError` / `VersionConflictError` / `PlanNotFoundError`, each
      carrying `code` + `details` already matching
      `$defs.error_details_plan_digest_mismatch`,
      `$defs.error_details_plan_version_conflict`,
      `$defs.error_details_state_not_found`.
      Codes and retryability from `tool_execution_contract.retryability_registry`
      — all three non-retryable. **Invent no new error code.**

- [x] **2.2 Test every raised error validates against its contract schema**
      `tests/unit/test_errors_schema.py`
      Validate with `jsonschema` against the bundled root `$defs`, exactly as
      `verify_contract.py` does. Assert `additionalProperties: false` is honoured
      (an extra details field must fail validation, proving the test can fail).

## Phase 3 — the store (depends on 1.1, 2.1)

- [x] **3.1 Implement `src/planpilot/store/plan_store.py`**
      `put_content` (write-once, recomputes and verifies digest before storing),
      `get_content`, `get_lifecycle`, `create_lifecycle`, `transition`,
      `supersede`, `latest_version`, `verify_digest`, `dump_state`, `load_state`.
      **Clock injected via `ts` argument — `datetime.now()` must not appear.**
      Validate stored content against `$defs.plan_content`.

- [x] **3.2 Test store invariants**
      `tests/unit/test_plan_store_invariants.py`
      write-once: different bytes raise, identical bytes idempotent ·
      corrupt plan rejected at `put_content` not at read ·
      unknown field rejected · `supersede` sets `SUPERSEDED` and records an event
      without mutating any approval set · injected clock reproducibility.

- [x] **3.3 Test the retention promise**
      `tests/unit/test_plan_store_persistence.py`
      `dump_state()` → `load_state()` → re-digest byte-identical.
      Dumped file is LF-only canonical JSON.

## Phase 4 — guard the closed vocabularies (depends on 3.1)

- [x] **4.1 Implement `tools/check_closed_vocabularies.py`**
      Scan every string literal under `src/` and `tests/`; any token matching a
      closed-vocabulary *shape* (`^[A-Z][A-Z0-9_]{3,}$`, `^HC-\d{3}$`,
      `^EVAL-\d{3}$`, `^EVT-\d{3}$`) that is absent from the contract's enums
      **fails the build**. This is the implementation-layer guard design.md
      assumes exists; the `PreTaskExec` hook is only a prompt and cannot enforce it.

- [x] **4.2 Test the vocabulary guard catches a fabricated identifier**
      `tests/unit/test_closed_vocabularies.py`
      Write a temp module containing `POLICY_VIOLATION` (valid, must pass) and
      `PLAN_DIGEST_MISMATCHED` (fabricated, must fail). Assert both outcomes —
      a guard that only ever passes is not a guard.

## Phase 5 — evidence (depends on all above)

- [x] **5.1 Negative control for the whole spec**
      `tests/negative_control/test_plan_store_negctl.py`
      Inject each defect class (float NaN allowed, non-canonical separators,
      digest includes lifecycle, write-once removed, clock read internally,
      fabricated code accepted) and require the corresponding test to fail.
      Report `caught=N escaped=0`.

- [x] **5.2 Write runtime evidence**
      `tests/evidence/plan-store-and-digest/`
      Timestamped: interpreter version, ortools version (unused here but recorded
      for the pack), fixture digests, test run summary, negative-control result.
      **A fixture digest committed here becomes the cross-language reference** any
      future reimplementation must match.

- [x] **5.3 Commit with the verification suite green**
      `python tools/generate_kiro_workspace.py` (steering follows contract) ·
      `python tools/validate_kiro_workspace.py` (190 checks) ·
      `python tools/negative_control_workspace.py` (14 caught) ·
      `python -m pytest tests/ -q`.

---

## Out of scope

No LLM calls, no Bedrock, no network, no dataset access, no hard-constraint
checking, no approval sets, no workflow state machine. See design.md §4.

## Definition of done

All five phases complete, `pytest` green, negative control `escaped=0`, and every
fixture digest recorded in `tests/evidence/`. **This does not make the contract
pilot-ready** — `release_readiness.runtime_evaluation` stays pending until
EVAL-001..030 run against real data.

---

## Verification record

Ticked 2026-09-12 after checking each task against its named artefact **and** its
substantive requirement, not just against the file existing. Four of the thirteen
were only nominally complete and were fixed before being ticked:

| Task | What was actually missing | Fix |
|---|---|---|
| 2.2 | The file existed and validated details, but the required negative half — "an extra details field must fail validation, proving the test can fail" — was absent, so every assertion in it was positive-only | `test_an_extra_details_field_fails_validation`, parametrised over all seven concrete error classes |
| 3.2 | `test_unknown_field_is_rejected_by_the_schema` asserted only `not fixtures.is_valid(...)` and never called `PlanStore`. It claimed the store rejects unknown fields while the store accepted them — the P0-1 defect hiding behind a green test | Now calls `put_content` and asserts the store is unchanged; plus `test_a_resigned_forged_plan_is_still_rejected`, where re-signing the forgery defeats the digest gate and only the schema gate can catch it |
| 4.2 | Used other contract members as equivalent samples rather than the two tokens this task names | `test_task_4_2_literal_case_both_outcomes_in_one_module` — `POLICY_VIOLATION` passes, `PLAN_DIGEST_MISMATCHED` fails, both in one module |
| 5.1 | 28 mutations covered floats, canonicalisation, digest identities, write-once, supersede, the P0 gates and `load_state` — but **not** "clock read internally" and **not** "fabricated code accepted", both named by this task | Two mutations added; 30 total, 29 caught / 0 escaped / 0 broken |

Note that 2.2 and 3.2 are the same defect shape as F13 (the ghost test): a file
that exists, is cited as a guarantee, and does not prove what it is cited for.
Existence is not completion.

### Evidence at tick time

```
pytest tests/ -q                              337 passed
negative control (store)                      caught=29 escaped=0 broken_fixtures=0 of 30
tools/check_closed_vocabularies.py            CLOSED VOCABULARY CHECK | PASS
tools/check_closed_vocabularies.py --self-test SELF-TEST | PASS
tools/generate_kiro_workspace.py              7 steering, 2 hooks, exit 0
tools/validate_kiro_workspace.py              ok=190 fail=0
tools/negative_control_workspace.py           caught=14 escaped=0 of 14
contract sha256                               b92e53f4ff054105... (unchanged)
```

`digest.py` imports only `hashlib`, `json`, `math`, `typing` — task 1.1's "pure
functions only, no clock, no file, no env read" was checked against the import
list rather than by grepping prose (the word "datetimes" appears in its
docstring and trips a naive text search).

All seven error codes declared by the store are members of
`tool_execution_contract.retryability_registry`, satisfying 2.1's "invent no new
error code".
