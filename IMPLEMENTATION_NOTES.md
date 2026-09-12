# Implementation Notes — plan-store-and-digest

Spec: `.kiro/specs/plan-store-and-digest/` (design.md, tasks.md)
Contract: `planpilot_agent_contract_v1.8.json` sha256 `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`
Evidence: `tests/evidence/plan-store-and-digest/EVIDENCE.json`

This file exists because commit `e2dea98` claimed the defects found while
building the spec were "all recorded in REVIEW notes" and no such file had been
written. The claim was made before the artefact existed. It exists now.

---

## Status

| | |
|---|---|
| unit tests | **259 passed** |
| negative control (store) | **18 caught / 0 escaped / 0 broken fixtures** of 19 |
| full suite (`pytest tests/`) | **262 passed** |
| closed-vocabulary guard self-test | **17/17 PASS** |
| closed-vocabulary guard repo scan | **PASS** (21 vocabularies, 135 members, 25 modules) |
| workspace validation | **190 checks / 0 fail** |
| workspace negative control | **14 caught / 0 escaped** |
| LLM / network / credentials / dataset used | **none** |

`release_readiness.runtime_evaluation` remains **pending**. This is unit evidence
for one module. It is not an EVAL result and does not make the contract
pilot-ready.

---

## Open finding requiring a contract decision

### F-STORE-01 — no registered error code describes an illegal lifecycle transition

**Situation.** `PlanStore.transition()` must refuse to move a `SUPERSEDED` plan,
and `create_lifecycle()` must refuse to recreate an existing record (which would
rewind status and approval binding). Neither condition has a usable code among the
contract's 18:

| candidate | why it does not fit |
|---|---|
| `VALIDATION_FAILED` | its details schema is the factory-state shape (`status`, `errors`, `quarantined_entity_count`) — describing input data validation, not a lifecycle transition |
| `POLICY_VIOLATION` | `violated_policy` is a closed enum of five values, all about materials, safety, untrusted data and publishing |
| `PLAN_VERSION_CONFLICT` | means "you expected a different version"; here expected == actual |

**Decision taken.** `TransitionNotAllowedError` and `LifecycleAlreadyExistsError`
derive from `StoreInvariantError`, which is **not** a `StoreError` and carries **no
contract code**. Rationale: both are caller programming errors. No user action
fixes them, so rendering them as a `tool_error` would report a bug as if it were a
planning outcome.

**What would change this.** If a legitimate runtime path ever needs to refuse a
transition *and tell the user*, the contract needs a new registered code with its
own `error_details_*` schema. That is a contract revision (v1.9), not something to
solve in code. Flagged for the Codex review.

**Pinned by** `test_invariant_errors_are_not_store_errors`, which asserts these
classes are not `StoreError` subclasses and carry no `code` attribute — so a future
contributor cannot quietly attach one.

---

## Defects found and fixed while building this spec

Each was found by a check, not by reading. Listed in the order found.

### D1 — `is_docstring` compared a `Constant` to an `Expr`

The closed-vocabulary guard intended to skip docstrings. In the AST a docstring is
`Expr(Constant)`, so `isinstance(parent, ast.Module)` never matched and **no
docstring was ever excluded**. The guard reported its own module docstring as a
violation. Fixed by precomputing docstring node ids via `_docstring_nodes()`.

### D2 — the guard's self-check 2 proved itself circularly

"Every allowlist entry must be used somewhere" scanned all modules including
`tools/check_closed_vocabularies.py`, whose allowlist *definition* literally
contains the tokens. It reported "all used" while
`tests/unit/test_closed_vocabularies.py` did not exist. Fixed by skipping
`NEGATIVE_FIXTURE_PATHS` in that check and by adding self-check 2b: an allowlist
entry scoped to a non-existent path is a hard failure.

That fix is what forced the test file to be written instead of the claim being
accepted.

### D3 — three dead allowlist entries

Found by D2's fix:

| entry | why it was dead |
|---|---|
| `PUBLISHING` | no underscore, so `TOKEN_RE` never matched it; it could not be a violation |
| `TO_BE_RECORDED_BEFORE_SUBMISSION` | **is** a contract member (collected from `hackathon_participation`), so allowlisting it hid a real vocabulary value from review |
| `NOT_A_REAL_STATUS` | appeared nowhere but in the list itself |

All three removed. `unused` was promoted from a printed note to a hard failure —
the note is precisely what let them ship.

### D4 — `acceptance_tests` entries are keyed `case_id`, not `id`

The guard crashed with `KeyError: 'id'` on first run. Fixed, and pinned by
`test_eval_cases_use_case_id_not_id` so a contract revision renaming the field
fails loudly rather than silently emptying the vocabulary.

### D5 — negative control polarity inverted (the serious one)

The first draft imported the mutated module and asked *"did the protection
hold?"*, recording `True` as "caught". That is backwards: `True` means the
mutation had **no** effect, i.e. the test did **not** detect it. It reported
**10 caught / 5 escaped** when the real numbers were close to the reverse.

Rewritten to the unambiguous method used by `negative_control_v18.py`: write the
mutation into the real source, run the real pytest suite as a subprocess, and
count the mutation as caught only if that suite **fails**. There is nothing left
to interpret. It also asserts `git status --porcelain src/planpilot/store` is
empty afterwards, so a crash cannot leave mutated sources behind.

### D6 — the two non-finite-float layers are not interchangeable (F-STORE-02)

D5's rewrite exposed this. The draft expected removing `_reject_non_finite` to be
harmless because `allow_nan=False` would still block NaN. Measured behaviour:

| layer | raises | carries |
|---|---|---|
| `_reject_non_finite` | `CanonicalizationError` | contract-shaped `details` (validates against `$defs.error_details_invalid_input`) **and** `json_path` |
| `allow_nan=False` | bare `ValueError` | `"Out of range float values are not JSON compliant"` — no details, no path |

So NaN stays blocked either way, but with layer 1 gone the error cannot become a
valid `tool_error`. That is a real regression. Pinned by
`test_layer1_error_is_not_the_same_as_layer2`, which asserts the two are different
exception types and that only ours has `.details`.

The negative control now probes three ways: layer 1 removed (suite **must** fail),
layer 2 removed (suite **must still pass** — layer 1 runs first), both removed
(suite must fail). The single "still green" case is the only genuine
defence-in-depth demonstration in the set.

### D7 — `CanonicalizationError.details` invented from assumption

Written as `{"field": ..., "reason": ...}` before checking the schema. The real
`$defs.error_details_invalid_input` requires `field_errors`,
`rejected_entity_type`, `rejected_entity_id` with `additionalProperties: false`,
and each `field_errors` item is a `validation_issue` needing six fields with `code`
from the 36-value enum. Caught by reading the schema, then pinned by
`test_error_details_match_the_contract_schema`.

This is why task 2.2 ("validate every raised error's details against its contract
schema") is worth its weight: the assumption was plausible and wrong.

### D8 — `pytest` imported but declared nowhere

The tests import `pytest`; `requirements.txt` did not list it. It had been
installed by hand, so a fresh clone could not run the suite. Fixed with
`requirements-dev.txt` rather than by editing `requirements.txt`, because the
latter is diff-verified identical to the contract's runtime pins and must not gain
a test runner (which would also land in the Lightsail image).

Verified by installing into a fresh venv from the two files alone and running the
suite — see the clean-environment result at the foot of this file.

### D9 — `parents[N]` / `parent` off-by-one, three times

`tests/_fixtures.py` used `parents[2]` and failed with `FileNotFoundError`. Fixed
by searching upward for `contract/`. Then `tools/write_evidence_plan_store.py` made
**the same mistake**. Then `tools/factcheck_impl_handoff.py` made it a **third**
time, using `Path(__file__).parent` and resolving to `tools/` instead of the repo
root.

Three occurrences is not bad luck, it is the wrong pattern. All three now search
upward for markers (`contract/` + `src/planpilot/`) instead of counting levels.

### D10 — a meaningless ternary left in shipped code

`VOCABULARIES["hard_constraint" if False else "error_code"]`. Harmless but it made
the line unreadable and suggested the author had not decided. Removed.

### D11 — `write_text` given bytes

A repair script called `p.write_text(s.encode("utf-8"))`. The file was untouched
(no half-written state), and the fix was `write_bytes` with explicit LF. Recorded
because it is the same class as the contract's CRLF bug: text-mode I/O on Windows
translates newlines.

### D12 — a redundant local import with a misleading comment

`assert_digest_consistent` had `from .errors import DigestMismatchError  # local
import: errors imports nothing here`. There was no circular-import problem, so the
import was dead weight and the comment was false. Moved to the module top.

### D13 — the guard flagged filenames as fabricated identifiers

`TOKEN_RE` ended with `(?![\w])`, so the string `"IMPLEMENTATION_NOTES.md"` yielded
the token `IMPLEMENTATION_NOTES` — a filename stem, not a vocabulary claim. Six
such references in `tools/factcheck_impl_handoff.py` made the repo scan fail.

The blast radius was the interesting part: `test_the_repository_passes_its_own_guard`
asserts a clean scan, so a guard false positive turned the **entire unit suite**
red. A precision bug in the guard is therefore more disruptive than a missed
fabrication.

Fixed with a `(?!\.\w)` lookahead — a token followed by a dot and a word character
is naming a file. A trailing sentence period still matches, because `"MISMATCHED."`
is a dot followed by whitespace, not by `\w`.

Pinned by `test_filename_references_are_not_scanned` (four filenames) and
`test_a_trailing_sentence_period_does_not_disable_detection`, so the lookahead
cannot be removed as "simplification" without a test failing.

**General lesson:** a guard that cries wolf gets overridden, and then it guards
nothing. Its precision is a functional requirement, not a nicety.

---

## External audit — findings F1–F15

An independent reviewer (a delegated subagent, given the same read-only access a
human auditor would have) audited this module and wrote `_audit_scratch/FINDINGS.md`
incrementally. **Every finding was reproduced against clean HEAD before it was
believed or fixed** — a subagent report is a claim, not a fact, and this project
has been burned by self-reported success.

The audit ran while I was concurrently editing the closed-vocabulary guard, so
its F14 ("repo tree is dirty") and its "guard tests failing" note were artifacts
of that concurrency, not store defects. The store findings are independent of it.

### Root cause shared by F2 and F5: a ghost test

`errors.py` line 10 and `tasks.md` line 50 both cited
`tests/unit/test_errors_schema.py` as the guard proving every error's `.details`
satisfies its contract schema. **That file had never been written.** It was the
exact mechanism that would have caught F2 and F5 on the first run — and it did
not exist. Cited-but-absent tests are the same orphan-spec class the V1.8
contract review found eight instances of. The file is now real (39 tests), and a
negative control proves it FAILS (6 tests, then 8) when F2 and F5 are
reintroduced, restoring byte-identical afterward.

### Findings, severity, and disposition

| # | sev | location | finding | disposition |
|---|---|---|---|---|
| F1 | HIGH | `load_state` | restored lifecycle records with NO validation — a hand-edited dump could inject a fake status, a digest disagreeing with its content, or an orphan lifecycle record | **FIXED** — validates status against the enum and digest against the bound content, before any insert; pinned by `TestF1LoadStateValidatesLifecycle` |
| F2 | HIGH | `PlanNotFoundError` | used `lookup_kind="plan"` (default) and `"plan_content_key"` (from `put_content`) — neither is in the schema enum, so every such `STATE_NOT_FOUND` was an unemittable tool_error | **FIXED** — `LOOKUP_KINDS` mirrored from the schema; out-of-enum refused at construction; malformed content now raises `InvalidContentError` (the correct code), not a lookup error |
| F3 | MED | `put_content:116` | idempotency compared `canonical_json`, which does NOT sort operations — a reordered-but-identical retry raised `IdempotencyConflictError` | **FIXED** — compares digests; pinned by `TestF3IdempotentRetryComparesDigestNotBytes` |
| F4 | LOW/MED | `sort_operations` | ops tied on all five contract fields kept input order (stable sort), so the digest depended on input order | **FIXED** — tie-broken by canonical JSON of the operation; measured before/after |
| F5 | HIGH | `DigestMismatchError` | built details with `str(None)` when the declared digest was absent, violating `^[a-f0-9]{64}$` — an unemittable tool_error | **FIXED** — malformed digest raises the new `InvalidContentError`; `DigestMismatchError` refuses non-64-hex inputs at construction |
| F6 | LOW/MED | `assert_digest_consistent` | `int(plan_version)` raised `ValueError` on a non-numeric version, masking the real `DigestMismatchError` | **FIXED** — `_safe_version()` for reporting only; pinned to assert the exact error type |
| F7 | info | `transition` | `PUBLISHED -> DRAFT` is accepted | **DOCUMENTED DEFERRAL** — the transition graph is `workflow.transitions`, not re-implemented here (design decision 3). The HIGH risk is publish-without-approval (F11), now closed; back-transition is a LOW hole by comparison |
| F8 | MED | `_resolve_version` | `version=None` silently resolves to newest across records | **MITIGATED** — mutations now require an explicit version (F11); reads may still default to latest by design |
| F9 | LOW | `drain_superseded` / `dump_state` | hands out raw internal dicts; a caller mutating them corrupts the next dump | **ACCEPTED** — single-process store; documented as a known gap (§ Not done). A defensive copy is cheap and worth adding if the store ever gains a second consumer |
| F10 | LOW | `transition` | cannot CLEAR `approval_set_id` / `published_version` (only set them) | **ACCEPTED** — no contract path requires clearing; flagged for the approval-service spec |
| F11 | HIGH | `transition:253` | `transition(plan_id, None, "PUBLISHED")` published the LATEST version regardless of which version an approval was bound to — a stale approval could publish a regenerated plan | **FIXED** — mutations require an explicit int version (bool rejected); pinned by `TestF11TransitionRequiresExplicitVersion` |
| F12 | MED | `sort_operations` | NOT total: `TypeError` on mixed-type key fields — contradicted design decision 4 | **FIXED** — `_total_key` type-rank wrapper (see decision 4 above); pinned by `TestF12SortOperationsIsTotalOnMalformedKeys` |
| F13 | LOW | tests | the default `lookup_kind` was never exercised | **FIXED as a side effect** — the ghost test now exercises every `LOOKUP_KINDS` member and the default |
| F14 | context | repo | tree dirty from my concurrent guard edits | **NOT A DEFECT** — confirmed: the auditor saw my in-progress edits, not a store bug |
| F15 | clean | negctl | negative-control method reviewed for false catches | **NO DEFECT FOUND** — method sound |

### What this changes about the digest

F4 (tiebreaker) and F12 (type rank) both touch `sort_operations`, so the
**canonical digest output changed** between the pre-audit and post-audit code.
The baseline `plan_content` digest is now `b9aa87e27b0e2c513d23…` (it was a
different value before). This is recorded in `tests/evidence/.../EVIDENCE.json`,
which also shows `baseline_plan_content.digest == shuffled_operations_10_seeds.digest`
— the direct evidence that F4 is fixed (shuffling operations no longer changes the
digest). No document hardcodes the old value, so nothing else drifted.

### The two things the audit caught that I would not have

1. **The ghost test.** I cited `test_errors_schema.py` in two places and never
   wrote it. I had run the suite green many times; nothing failed, because a
   test that does not exist cannot fail. Only an external reader noticed the
   citation pointed at nothing.
2. **F12 contradicting my own design decision 4.** I had *written* "the digest
   layer stays total" as a selling point. It was not true. I believed my own
   prose over the code until the audit forced a reproduction.

---

## Design decisions worth challenging in review

1. **Digest verified on write, not on read.** A fabricated plan never enters the
   store. Costs one SHA-256 per write; buys the property that everything in the
   store is already consistent.
2. **`supersede()` is deliberately non-idempotent.** A second call raises.
   `drain_superseded()` is an exactly-once hand-off to the approval service; a
   duplicated event would invalidate the same set twice.
3. **The transition graph is not re-implemented here.** `workflow.transitions` owns
   it (11 states); lifecycle status is a projection. Only terminality is enforced.
   A second copy of the graph is exactly the orphan-spec defect class the V1.8
   review found eight instances of.
4. **`sort_operations` is TOTAL — it never raises on malformed keys.** Schema
   validation belongs to `validate_factory_state` / `validate_plan`. The digest
   layer stays total so a malformed operation still produces a deterministic
   digest and the failure surfaces where it can be reported properly.
   **This was aspirational until audit finding F12 proved it false:** mixed-type
   sort fields (`lot_no` 1 vs `"1"`) raised `TypeError`, so a malformed plan could
   not be digested at all. Now each key field is wrapped in `_total_key` →
   `(type_rank, value)`, which short-circuits cross-type comparison on the rank.
   For well-formed homogeneous input the rank is constant per position, so
   ordering — and the baseline digest `b9aa87e2…` — is unchanged. Pinned by
   `TestF12SortOperationsIsTotalOnMalformedKeys`.
5. **Canonical form pins `ensure_ascii=False`.** Non-ASCII hashes as itself. With
   `True`, every CJK string would expand to `\uXXXX` escapes and any second
   implementation that guessed differently would disagree. The evidence pack
   records `utf8_byte_len` so a reimplementation can check.
6. **`int` and `float` are not unified.** `$defs.kpis` separates `integer` from
   `number`, so `1` and `1.0` must hash differently. Pinned and recorded in the
   evidence pack.

---

## Clean-environment verification

Executed, not asserted. A fresh venv was created with no packages, then:

```
python -m venv cleanenv
cleanenv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
cleanenv/Scripts/python.exe -m pytest tests/ -q
```

Result at the time of that run: **152 passed in 27.71s**, exit code 0. The suite
has since grown (audit fixes + the ghost test) to 262 unit + 6 negative control;
re-run `pytest tests/` for the current count rather than trusting this number. The install pulled only what the two requirements files
declare (plus their transitive deps), so a teammate cloning this repo can
reproduce it.

This is the answer to "can someone else run my agent's tests" — a recorded result
rather than a claim. Note the temp venv was deleted afterwards; it is not part of
the repo.

---

## Not done (next specs)

- `tool-error-middleware` — the 18 `details` schemas, UUIDv4 correlation ids, the
  4096-byte envelope
- `audit-hash-chain` — append-only SHA-256 chain with the genesis sentinel
- `approval-service` — consumes `drain_superseded()`; atomic set creation,
  snapshot invariants, TTL clamping
- `dataset-migration` — the critical path for EVAL-020..030
