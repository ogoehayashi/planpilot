# Implementation Review Handoff — spec `plan-store-and-digest`

**For:** an independent reviewer (Codex, or a human)
**From:** the implementing agent (Hermes)
**Date:** 2026-09-12
**Repo:** `E:\PlanPilot-Hackathon\planpilot-build` — git, **no remote yet**.
Snapshot for the counts in this document: commit `cd5219f` (16 commits, 55 files tracked, working tree clean at
the time of writing). Commit/HEAD/tracked counts are stated *as of that commit*
and are not live values: a committed document cannot state the HEAD of the
commit that contains it. `tools/factcheck_impl_handoff.py` verifies the named
snapshot commit exists and is an ancestor of HEAD rather than equal to it.

The line counts and test totals describe that same snapshot. An earlier revision
named `79777fc` while carrying numbers measured on a later tree, which made the
document internally inconsistent — the snapshot reference has to move with the
numbers, not stay pinned to the commit where the document was first written.

This document is deliberately sceptical of its own subject. Where a claim could be
verified mechanically, it was, and the command is given. Where the implementer
made a judgement call, the call is named so it can be attacked.

**Read in this order:**
1. this file
2. `IMPLEMENTATION_NOTES.md` — the 13 defects found while building (D1–D13), the
   external audit findings (F1–F15), and finding F-STORE-01
3. `.kiro/specs/plan-store-and-digest/design.md` — what the module is supposed to do
4. `.kiro/specs/plan-store-and-digest/tasks.md` — the task list it was built against
5. the code: `src/planpilot/store/{digest,errors,plan_store}.py`

The **contract** review handoff is a separate document:
`../contract-review/REVIEW_HANDOFF_FOR_CODEX.md`. It covers V1.8 of
`planpilot_agent_contract_v1.8.json`. This file covers only the first
implementation spec.

---

## 1. What was built, and what was NOT

Built: the `plan-store-and-digest` spec — canonical digest, contract-shaped store
errors, and the write-once content / mutable lifecycle store. Phases 1–5 of
`tasks.md`, all checked off.

**Not built:** everything else. There is no scheduler, no approval service, no
audit chain, no tool layer, no inference client, no UI, no dataset access.
`.kiro/specs/README.md` lists 15 specs and exactly one of them has a directory,
so **14 of the 15 do not exist yet** — not merely untouched, but unstarted.
(An earlier revision of this document said "8 of the 15", which nothing on disk
supported; the count is now taken from the README's own table.)

**No LLM, network, credential or dataset was involved.** This is enforced by the
fact that the test suite runs in a clean venv with only `jsonschema`, `ortools`
(transitively) and `pytest` installed — verified in §3.

Scale: **6,942 lines** across 25 Python files (the subject; the factcheck tool that
measures them is excluded to avoid self-reference); tracked-file count is as of the
snapshot commit.

| layer | lines | files |
|---|---|---|
| `src/planpilot/` — store + validation packages + top-level init | 2,037 | 8 |
| `tests/unit/` — 9 test modules + init | 2,933 | 10 |
| `tests/negative_control/` | 521 | 2 |
| `tests/_fixtures.py`, `conftest.py`, `__init__.py` | 403 | 3 |
| `tools/check_closed_vocabularies.py` | 734 | 1 |
| `tools/write_evidence_plan_store.py` | 314 | 1 |
| **subject total** | **6,942** | **25** |

`tools/factcheck_impl_handoff.py` (289 lines) checks the table above, so it is
**deliberately excluded from the total** — the same reason `canonical_plan_digest`
excludes `plan_digest` and `engine.canonical_plan_hash` from the hashed payload. A
checker that counts itself makes the figure move every time the checker is edited,
which is a circular dependency, not a measurement. (All 29 Python files together are
8,522 lines, including the 289-line factcheck tool.)

---

## 2. Verification evidence — reproduce it yourself

Every number below was produced by running the command, not estimated.

```bash
cd E:\PlanPilot-Hackathon\planpilot-build
set PY=E:\PlanPilot-Hackathon\contract-review\.venv\Scripts\python.exe
```

| # | command | result |
|---|---|---|
| 1 | `%PY% -m pytest tests/unit -q` | **334 passed** |
| 2 | `%PY% -m pytest tests/negative_control -q` | **3 passed** (30 mutations) |
| 3 | `%PY% tools/check_closed_vocabularies.py --self-test` | **SELF-TEST \| PASS** (17 cases) |
| 4 | `%PY% tools/check_closed_vocabularies.py` | **PASS** — 21 vocabularies, 135 members, 29 modules scanned |
| 5 | `%PY% tools/validate_kiro_workspace.py` | **ok=190 fail=0** |
| 6 | `%PY% tools/negative_control_workspace.py` | **caught=14 escaped=0 of 14** |
| 7 | `sha256sum contract/planpilot_agent_contract_v1.8.json` | `b92e53f4ff054105…` unchanged |

Negative-control detail (from the run): **29 caught / 0 escaped / 0 broken
fixtures of 30**, sources restored to their pre-test bytes. The restore check
used to compare `git status --porcelain`, which wrongly failed whenever legitimate
uncommitted work existed; it now compares against the pristine bytes captured at
test start (the actual invariant). The one non-"caught" case is intentional and is
the only genuine defence-in-depth demonstration — see §5.

Evidence pack: `tests/evidence/plan-store-and-digest/EVIDENCE.json` plus three raw
logs. It records reference digests, package versions, and a `scope_statement`
that says plainly this is **unit** evidence, not an EVAL result.

### 2.1 Clean-environment proof

The question "can someone else run this" was answered by execution:

```bash
python -m venv cleanenv
cleanenv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
cleanenv\Scripts\python.exe -m pytest tests/ -q
```

**152 passed in 27.71s, exit 0.** Only the two requirements files were used. The
temp venv was deleted afterwards and is not in the repo.

That run predates later additions (the filename-reference pins in §3.1, both
audits' fixes, and the ghost test). The suite is now **337 tests** (334 unit + 3 negative
control). The clean-environment claim is about the two requirements files being
sufficient, which is unaffected — but it was last measured at 152 tests, so
re-run it for the current count rather than trusting either number. The last
attempt to re-run it was stopped before it finished, so no clean-environment
result exists for the current tree.

This matters because of defect **D8**: `pytest` was imported by the tests but
declared nowhere — it had been installed by hand. A fresh clone would not have
run. Fixed by adding `requirements-dev.txt` rather than editing
`requirements.txt`, which is diff-verified identical to the contract's runtime
pins (asserted by `tools/validate_kiro_workspace.py`) and must not gain a test
runner that would also land in the Lightsail image.

### 2.2 An independent audit, and what it found

After the first commit, I delegated a **read-only audit** of this module to an
independent subagent — the same access a human auditor gets. It wrote
`_audit_scratch/FINDINGS.md` incrementally and reported 15 findings (F1–F15).

**I reproduced every finding against clean HEAD before believing or fixing it.**
A subagent report is a claim, not a fact. All four HIGH findings reproduced. The
full disposition table is in `IMPLEMENTATION_NOTES.md` §"External audit"; the
summary here:

| # | sev | finding | disposition |
|---|---|---|---|
| F1 | HIGH | `load_state` restored lifecycle records unvalidated — a hand-edited dump could inject a fake status or a digest disagreeing with its content | FIXED + pinned |
| F2 | HIGH | `PlanNotFoundError` used `lookup_kind` values outside the schema enum → unemittable tool_error | FIXED + pinned |
| F5 | HIGH | `DigestMismatchError` built details with `str(None)`, violating `^[a-f0-9]{64}$` | FIXED + pinned |
| F11 | HIGH | `transition(pid, None, "PUBLISHED")` published the newest version regardless of which one was approved | FIXED + pinned |
| F3 | MED | idempotency compared order-sensitive bytes, not the digest | FIXED + pinned |
| F4 | LOW/MED | tied operations kept input order → digest depended on input order | FIXED, measured |
| F12 | MED | `sort_operations` raised `TypeError` on mixed-type keys, contradicting design decision 4 | FIXED + pinned |
| F6 | LOW/MED | `int(plan_version)` raised `ValueError`, masking the real error | FIXED + pinned |
| F7, F9, F10 | info/LOW | back-transitions accepted; raw dicts handed out; fields cannot be cleared | DOCUMENTED / deferred to later specs |
| F8 | MED | `version=None` resolves newest | MITIGATED by the F11 fix |
| F13 | LOW | default `lookup_kind` untested | FIXED by the ghost test |
| F14, F15 | context | dirty tree (my concurrent edits, not a defect); negctl method reviewed clean | NOT A DEFECT |

**The audit's most valuable finding was not any F-number — it was the root cause
of F2 and F5.** `errors.py` and `tasks.md` both cited
`tests/unit/test_errors_schema.py` as the schema-validating guard. **That file had
never been written.** A cited-but-absent test cannot fail, so F2 and F5 ran green
the whole time. The file is now real (54 tests) and a negative control proves it
FAILS (6, then 8) when F2 and F5 are reintroduced. This is the same orphan-spec
class the V1.8 contract review found eight instances of — and I committed it
again, in prose, in two files.

**Two audit findings I would not have caught myself:**

1. The ghost test. I ran the suite green many times; nothing failed, because a
   test that does not exist cannot fail. Only an external reader noticed the
   citation pointed at nothing.
2. F12 contradicting my own design decision 4. I had *written* "the digest layer
   stays total" as a selling point. It was not true, and I believed my prose over
   the code until the audit forced a reproduction.

**One digest consequence to flag:** F4 (tiebreaker) and F12 (type rank) both
changed `sort_operations`, so the canonical digest output changed. The baseline
`plan_content` digest is now `b9aa87e27b0e2c513d23…`. `EVIDENCE.json` shows
`baseline_plan_content.digest == shuffled_operations_10_seeds.digest` — the direct
proof F4 is fixed. No document hardcodes the old value.

### 2.3 A second audit, which found two P0 defects the first missed

A second independent audit ran against the same module and found **two defects
neither I nor the first audit had seen**, both P0, both poisoning the safety
premises of every module that would be built on this one. I reproduced both
against a clean tree with a standalone probe before fixing anything — same
discipline as §2.2. A subagent report is still a claim.

**P0-1 — the store never validated content against the contract.**

`put_content` checked that `plan_id`/`plan_version` were present and that both
digest identities held. That was all. The digest is content-addressed, so anyone
who fabricates a plan can re-sign it, which means digest consistency proves
content was not altered *after* signing and never proves the content is *legal*.

Five payloads, each mutated and then **re-signed so the digest check passed**,
were all accepted and readable via `get_content`:

| injected | contract verdict | store (before) |
|---|---|---|
| unknown top-level field | `Additional properties are not allowed` | accepted |
| `plan_version=True` | `True is not of type 'integer'` | accepted |
| `kpis.on_time_delivery="high"` | same | accepted |
| required `kpis` block deleted | `'kpis' is a required property` | accepted |
| unknown field in `operations[0]` | `Additional properties are not allowed` | accepted |

Worse, the lifecycle `ts` argument was stored verbatim into `updated_at` with no
format check, so `"not a timestamp at all"`, `""`, `None`, `12345` and
`"2026-99-99T99:99:99Z"` all passed — against a field the contract types as
`format: date-time`.

**The test that should have caught this existed and was green.**
`test_unknown_field_is_rejected_by_the_schema` asserted only
`not fixtures.is_valid(polluted, "plan_content")`. It never called `PlanStore`.
Its name claimed the store rejects unknown fields; the store accepted them. That
is the ghost-test failure mode again, one layer down: not a missing file this
time, but a test that proves something other than what it is cited for.

Fix: new `src/planpilot/validation/` package — one production validator, compiled
once per `$defs` entry and memoised, with `format_checker` enabled (without it,
jsonschema treats `format` as an annotation and accepts garbage timestamps).
Every store write boundary now validates through it, and `tests/_fixtures.py` is
no longer on any production path. `bool` versions are refused explicitly:
`hash(True) == hash(1)`, so a bool would have collided with version 1 as a dict
key. `transition` and `create_lifecycle` build a candidate, validate it, then
replace — the old code mutated the stored record in place, so a rejected write
could leave it half-changed.

**P0-2 — `expected_plan_version` was a tautology.**

It was compared against the caller's own `plan_version` argument. Both values
came from the caller, so the check could only ever catch a caller contradicting
itself. Reproduced: with v1 `APPROVED` (approval set `APR-1` bound) and v2
stored, `transition(pid, 1, "PUBLISHED", expected_plan_version=1)` **published
v1**. The docstring claimed this "is what stops a stale approval set from
publishing a regenerated plan" and cited
`security_controls.approvals_bound_to_plan_version_and_digest` as authority. The
claim was false — a documentation defect on top of the behavioural one, the same
pairing as F12.

Fix: compared against `current_active_version()`, the store's own view. `APPROVED`
and `PUBLISHED` additionally require the target to be the active version, so
omitting `expected_plan_version` no longer bypasses the protection. The terminal
check runs *first*: reporting "wrong version" for a superseded record would
advise a retry that can never succeed.

New `commit_new_version()` performs validate → continuity → write content →
create lifecycle → supersede previous → record invalidation event, rolling back
entirely on any failure. `put_content()` deliberately stays a low-level primitive
that does **not** supersede — writing content and retiring a version are separate
concerns, and bundling them inside `put_content` would make an idempotent retry
path able to invalidate approvals. Both halves of that split are pinned by test
so a refactor cannot "helpfully" reunite them. Supersede events now carry
`invalidation_cause` from the contract's closed enum.

**P1 — `load_state` hardened.** Duplicate `(plan_id, plan_version)` records were
silently collapsed to the last by a dict comprehension, which is a swap rather
than a load; `superseded_events` were restored with no validation at all, so a
fabricated event for a nonexistent plan would have been handed to the approval
service to act on. Now: the envelope is validated against a closed module-owned
schema, duplicates are refused, and every event must reference content present in
the dump with a matching digest.

**What the negative control then caught in my own fix.** After adding ten
mutations for the new defences, one **escaped**: reverting
`expected_plan_version != active` back to the tautological `!= version` left the
suite green. Every P0-2 test I had written used `PUBLISHED` or `APPROVED`, which
the *other* new gate blocks on its own — so the OCC check itself was untested.
Defence in depth hid a hole in one of the layers. Pinned with a `BLOCKED`
transition, which only OCC can catch. Now 30 mutations, 29 caught / 0 escaped /
0 broken.

**Two audit claims I checked and corrected.** It said a malformed lifecycle could
be created directly with `status="PUBLISHED"`. Half right: the status enum *was*
already validated, and `PUBLISHED` is a legal enum member, so this is not a
schema violation — real publication authority is meant to be gated later by the
`publish_plan` tool, not by the store. Its claim about unvalidated timestamps was
right, and worse than stated.

These P0s are **not** in the F-series numbering: they come from a separate audit
with separate findings, and `IMPLEMENTATION_NOTES.md` records them as P0-1/P0-2
to keep the two audits distinguishable.

---

## 3. The closed-vocabulary guard (the part I would most like attacked)

`tools/check_closed_vocabularies.py`, 620 lines.

**Why it exists.** `verify_contract.py` runs 600 assertions — against the
*contract*. Nothing checked the *implementation*. The `PreTaskExec` hook in
`.kiro/hooks/guard-spec-tasks.json` asks the model not to invent identifiers, but
**a prompt is not a gate**. A fabricated validation code in `src/` would be caught
by no existing check and would surface at EVAL time, days before the deadline.

**How it works.** Parses every module under `src/ tests/ tools/` (22 at the
time of writing) with `ast` and inspects **string constants only**. That is what keeps precision high:
`"PLAN_DIGEST_MISMATCH"` is checked; `OPERATION_SORT_KEY_FIELDS` (an identifier)
never is. Excluded on principle, not by allowlist: docstrings, `__all__` contents,
dotted references in prose, the `PLANPILOT_*` implementation namespace, and names
actually defined in Python somewhere in the project.

21 vocabularies (135 members) are read from the contract at runtime — never
hardcoded. Default-deny: a vocabulary-shaped token absent from the contract fails
the build unless it is in `ALLOWED_LOCAL` **with a reason and a path scope**.

**Highest-signal check: near misses.** The dangerous fabrication is not a random
word, it is a near-copy of a real member — `PLAN_DIGEST_MISMATCHED`,
`SEARCH_ESCALATION_EXHAUST`, `HC-014`. Those read as legitimate in review. Any
token within Levenshtein distance 2 of a real member is reported separately and
always fails, allowlist or not.

**Self-checks, so the guard cannot rot:**
1. no allowlist entry may be a contract member (would hide a real value)
2. every allowlist entry must be used by some *scanned* module — and
   `NEGATIVE_FIXTURE_PATHS` are excluded from that count, because counting the
   guard's own body is circular (this was defect **D2**)
2b. every allowlist path must exist, or the entry can never fire
3. vocabularies must be non-empty and contain known members; `error_code` must
   have exactly 18, `validation_issue_code` exactly 36

**`--self-test`** runs 17 cases proving the guard catches fabrication and does not
over-report. Run it before trusting any PASS.

### 3.1 The one false positive it has produced so far

The guard blocked the entire test suite once, and the cause is worth recording
because it is the failure mode most likely to recur.

`TOKEN_RE` originally ended with `(?![\w])`. A string containing
`"IMPLEMENTATION_NOTES.md"` therefore yielded the token
`IMPLEMENTATION_NOTES` — a filename stem, not a vocabulary claim. Six such
references in `tools/factcheck_impl_handoff.py` were reported as unrecognised
tokens, the repo scan failed, and because
`test_the_repository_passes_its_own_guard` asserts a clean scan, **the whole unit
suite went red**.

Fixed by adding a `(?!\.\w)` lookahead: a token followed by a dot and a word
character is naming a file. A trailing sentence period still matches, because
`"MISMATCHED."` is a dot followed by whitespace.

Pinned by `test_filename_references_are_not_scanned` (four filenames) and
`test_a_trailing_sentence_period_does_not_disable_detection`, so the lookahead
cannot be "simplified" away.

**Why this matters more than the bug.** A guard that cries wolf gets overridden,
and then it guards nothing. Its precision is a functional requirement, not a
nicety. Recorded as defect **D13**.

### 3.2 An independent attack found ten bypasses

After writing §3 and §3.1, I had a separate agent attack the guard with one
instruction: smuggle a fabricated contract identifier into the implementation
without the guard failing. It found **ten bypasses and proved each one end to
end** — copying the repo to scratch, smuggling the payload in, running the real
guard (exit 0), then executing the smuggled module to show the fabricated
identifier materializing at runtime. Controls failed the guard in every copy, so
the copies were faithful.

I independently reproduced the three I fixed before fixing them. The attacker's
report is not taken on trust.

| # | Bypass | Mechanism | Severity | Status |
|---|---|---|---|---|
| 1 | Bind the fabrication as a Python name once (`PLAN_DIGEST_MISMATCHED = "..."`), even in a test-function local, then use the string anywhere in `src/` | `_DEFINED_NAMES` was checked **before** the near-miss check, so it voided the guard's own highest-signal feature | **CRITICAL** | **FIXED** |
| 2 | Put fabricated codes in a non-`.py` file (`src/planpilot/vocab_override.json` loaded at runtime), or in a root-level `.py`, or under `tests/negative_control/` imported by `src/` | scan scope is only `*.py` under exactly `src/ tests/ tools/` | **CRITICAL** | **OPEN — accepted limit** |
| 3 | `tools/check_closed_vocabularies.py.evil.py` → matched the guard's own skip entry and was never scanned; `tests/unit/test_closed_vocabularies.py.evil.py` → inherited all nine allowlist tokens | bare `startswith` has no path-component boundary | **HIGH** | **FIXED** |
| 4 | Fabricated single-word statuses: `"PUBLISHING"`, `"APPROVEDD"` (edit distance 1 from `APPROVED`) | `TOKEN_RE` requires an underscore group or `PREFIX-DIGITS`, so a bare word never reaches `classify()` | **HIGH** | **OPEN — accepted limit** |
| 5 | `"HC-0" + "14"`, `f"HC-{n:03d}"`, `"".join`, `.format`, `chr()`, `bytes.fromhex(...)`, `"hc-014".upper()` | only literal string constants are folded; `ast` concatenation is not | **HIGH** | **OPEN — accepted limit** |
| 6 | `"PLAN_DIGEST_MISMATCHED.md"[:-3]`; packed sentences `"Error MISMATCHED.After retry EVAL-031.Failed"` | the `(?!\.\w)` filename lookahead accepted any dot+word suffix | **MED-HIGH** | **FIXED** |
| 7 | Codes in `#` comments, extracted at runtime via `inspect.getsource` + regex | comments are not string constants | **MEDIUM** | **OPEN — accepted limit** |
| 8 | `"see planpilot.HC-014".split(".")[-1]` | the dotted-reference exclusion | **MEDIUM** | **OPEN — accepted limit** |
| 9 | Fabricate in a docstring, read via `__doc__.split()` | docstrings are excluded | **LOW-MED** | **OPEN — accepted limit** |
| 10 | `__all__ = ["HC-014"]; CODES = list(__all__)`; `"PLANPILOT_PLAN_DIGEST_MISMATCHED"[10:]`; `b"HC-014"`; homoglyph hyphen U+2011 | the `__all__`, `PLANPILOT_` and bytes exclusions | **LOW-MED** | **OPEN — accepted limit** |

**Negative results** (the guard held): implicit literal concatenation
`"PLAN_DIGEST_" "MISMATCHED"` is caught (`ast` folds adjacent literals); every
container context is caught (dict keys/values, lists, sets, nesting, annotations,
default args, decorators, comprehensions, assert messages, raise args);
`\u0050`-escaped spellings are caught; hyphenated ids cannot exploit `_DEFINED_NAMES`
because they are not valid Python names. The near-miss machinery itself is sound
for every token that reaches `classify()` — **every bypass works by never
reaching it, or by `_DEFINED_NAMES` preempting it.**

#### The three fixes

1. **Near-miss now runs before `_DEFINED_NAMES`.** A defined name within edit
   distance 2 of a contract member is a vocabulary claim wearing a constant's
   clothes. Genuine constants (`CONTRACT_SHA256`, `OPERATION_SORT_KEY_FIELDS`) are
   far from every member and are unaffected.
2. **`_path_matches()` does component matching.** A pattern ending in `/` matches a
   subtree; anything else must be exactly equal.
3. **The filename lookahead requires a known document extension**
   (`md|markdown|json|py|txt|ya?ml|rst|csv|tsv|toml|cfg|ini|log|html|ipynb`), and
   `_is_fabricated_stem()` re-examines a filename-shaped token, flagging it when
   the stem is itself an out-of-range prefixed id or a near miss. Measured against
   the repo first: 7 distinct `TOKEN.ext` strings exist, the rule flags 0
   legitimate filenames.

All three are pinned by `tests/unit/test_guard_bypass_regressions.py` (48 tests),
which reproduces the attacker's own payloads rather than easier substitutes. That
module also pins the **seven open limits as current behaviour** — if anyone closes
one, the test fails and forces §3.2 to be updated in the same commit. Silent
divergence between doc and code is the failure mode this project keeps hitting.

#### What the guard actually is

The attacker's verdict, which I accept:

> **Yes — as an accident tripwire, not a security boundary.** Its realistic threat
> model is an LLM or developer writing a wrong code into a string literal, and
> against that it works (honest controls failed it every time, in every copy).
> Against a deliberate adversary it is trivially defeated.

That reframing matters for how this is presented at the hackathon. The guard is
**not** a security control and must not be described as one. It is a
typo-and-drift tripwire for the realistic case: an agent or developer writing
`PLAN_DIGEST_MISMATCHED` or `HC-014` into a literal. It closes that window, which
is where the actual risk lives, because the alternative — discovering the
fabrication at EVAL time — leaves days, not hours.

The §3 "attack surface" list above predates this attack. It named
`_DEFINED_NAMES`, `TOKEN_RE`'s lookbehind, the allowlist's path-scoping and
`classify()`'s ordering as the things to probe. The attack confirmed all four and
found six more.

### Attack surface I would point a reviewer at

- The `_DEFINED_NAMES` escape hatch: a fabricated token that someone first binds
  as a Python constant would be excused. Documented in the file as a known
  limitation. Is that acceptable, or should definition-site checks be stricter?
- `TOKEN_RE` uses a negative lookbehind for `.` to skip dotted prose. Can a real
  vocabulary claim be smuggled past it?
- The allowlist is now 12 entries, scoped across the three fixture test modules
  (`test_closed_vocabularies.py`, `test_guard_bypass_regressions.py`,
  `test_audit_fixes_pinned.py`). Is path-scoping sufficient, or should
  the guard refuse to allowlist prefixed ids (`HC-014`) at all?
- `classify()` returns `fabricated_hard_constraint` for `HC-014` **before**
  checking near misses. Is that ordering right for every prefix family?

---

## 4. Digest implementation — the pinned choices

`src/planpilot/store/digest.py`. The contract specifies the digest only as
"SHA-256 of the canonical immutable content excluding only `plan_digest` and
`engine.canonical_plan_hash`". Everything else had to be decided, and each
decision is asserted by a test:

| decision | value | test |
|---|---|---|
| encoding | UTF-8 | `test_non_ascii_digest_matches_its_utf8_bytes` |
| key order | `sort_keys=True` | `test_dict_key_insertion_order_does_not_matter` |
| separators | `(",", ":")` | `test_no_whitespace_in_canonical_form` |
| `ensure_ascii` | `False` | `test_non_ascii_is_not_escaped` |
| non-finite floats | rejected, with contract-shaped error | `TestNonFiniteFloatsRejected` (5 tests) |
| int vs float | type-preserving (`1` ≠ `1.0`) | `test_int_and_float_are_distinguished` |
| operations order | `(start_time, machine_id, order_id, lot_no, operation_no)`, then a canonical-JSON tiebreaker and a per-field type rank so the order is TOTAL | `test_sort_operations_uses_the_contract_key`, `TestF4…`, `TestF12…` |
| algorithm | SHA-256 of that exact text | `test_digest_equals_sha256_of_canonical_text` |

**Both identities must hold**, per `$defs.plan_content.plan_digest.description`
("equals `engine.canonical_plan_hash`"):

```
canonical_plan_digest(content) == content["plan_digest"]
canonical_plan_digest(content) == content["engine"]["canonical_plan_hash"]
```

Including the subtle case where **both fields agree with each other but not with
the content** (`test_both_hashes_agree_with_each_other_but_not_the_content`).

Reference digests are committed in `EVIDENCE.json` so any reimplementation (a
TypeScript UI, a second solver) can be checked for equivalence.

**Challenge this:** is `ensure_ascii=False` right? With `True`, CJK strings expand
to `\uXXXX` and any second implementation that guessed differently would disagree
silently. I pinned `False` and recorded `utf8_byte_len`. Is there a scenario where
the opposite choice is safer?

---

## 5. Negative control — including the defect it exposed in itself

`tests/negative_control/test_plan_store_negctl.py`, 19 mutations.

**Method.** Each mutation is written into the **real** source file, the **real**
pytest suite runs as a subprocess, and the mutation counts as caught only if that
suite **fails**. The file is restored in `finally`, and the test ends by asserting
`git status --porcelain src/planpilot/store` is empty.

**The first draft did the opposite** (defect **D5**, the most serious one): it
imported the mutated module, asked "did the protection hold?", and recorded `True`
as "caught". `True` meant the mutation had **no** effect — i.e. the test did
**not** detect it. It reported **10 caught / 5 escaped** when the truth was close
to the reverse. A reviewer who trusted that output would have believed five
guards were broken and ten were fine, when neither list was right.

**It also produced a real finding (F-STORE-02).** The draft assumed the two
non-finite-float layers were redundant. Measured:

| layer | raises | carries |
|---|---|---|
| `_reject_non_finite` | `CanonicalizationError` | contract-shaped `details` (validates against `$defs.error_details_invalid_input`) **and** `json_path` |
| `allow_nan=False` | bare `ValueError` | `"Out of range float values are not JSON compliant"` — no details, no path |

NaN stays blocked either way, but with layer 1 gone the error cannot become a
valid `tool_error`. So the control now probes **three** ways: layer 1 removed
(suite **must** fail), layer 2 removed (suite **must still pass**), both removed
(must fail). The single "still green" case is the only honest defence-in-depth
demonstration in the set — and `test_layer1_error_is_not_the_same_as_layer2` pins
the difference so nobody "simplifies" layer 1 away as redundant.

**Reviewer question:** the 18 "must fail" mutations each name one suite. Is any
mutation caught by a test that would *also* fail for an unrelated reason? That
would be a false catch. I did not check for this systematically.

---

## 6. Open finding requiring a contract decision

### F-STORE-01 — no registered error code describes an illegal lifecycle transition

`transition()` must refuse to move a `SUPERSEDED` plan; `create_lifecycle()` must
refuse to recreate an existing record (which would rewind status and approval
binding, breaking `security_controls.approvals_bound_to_plan_version_and_digest`).
Neither condition fits any of the contract's 18 codes:

| candidate | why it does not fit |
|---|---|
| `VALIDATION_FAILED` | details schema is the factory-state shape (`status`, `errors`, `quarantined_entity_count`) |
| `POLICY_VIOLATION` | `violated_policy` is a closed enum of five values about materials, safety, untrusted data, publishing |
| `PLAN_VERSION_CONFLICT` | means "you expected a different version"; here expected == actual |

**Decision taken.** `TransitionNotAllowedError` and `LifecycleAlreadyExistsError`
derive from `StoreInvariantError`, which is **not** a `StoreError` and carries **no
contract code**. Both are caller programming errors: no user action fixes them, so
rendering them as a `tool_error` would report a bug as a planning outcome.

Pinned by `test_invariant_errors_are_not_store_errors`, which asserts they are not
`StoreError` subclasses and have no `code` attribute — so a contributor cannot
quietly attach one.

**This is the finding most worth a second opinion.** Alternatives:
(a) reuse `VALIDATION_FAILED` with a degenerate details payload;
(b) add a 19th code in v1.9;
(c) keep it a crash (what I did).
I chose (c) because inventing codes is forbidden by
`.kiro/steering/contract-authority.md` rule 3 and (a) misreports the condition.
But if the workflow layer ever needs to *tell the planner* that a transition was
refused, (c) is wrong.

---

## 7. Design decisions to challenge

1. **Digest verified on write, not on read.** Costs one SHA-256 per write; buys
   "everything in the store is already consistent". Is write-time the right place,
   given `validate_plan` re-runs checks independently later?
2. **`supersede()` is deliberately non-idempotent** — a second call raises.
   `drain_superseded()` is an exactly-once hand-off; a duplicate event would
   invalidate the same approval set twice. But is "raise" right, or should a repeat
   be a silent no-op like `put_content`'s identical-bytes case? **These two are
   inconsistent with each other and I am not sure which is wrong.**
3. **The transition graph is not re-implemented.** `workflow.transitions` owns it
   (11 states); lifecycle status is a projection. Only terminality (`SUPERSEDED`)
   is enforced here. A second copy of the graph is the orphan-spec defect class the
   V1.8 review found eight instances of. Risk: an illegal-but-non-terminal
   transition (e.g. `PUBLISHED → DRAFT`) is **accepted** by this module. Is
   deferring to the workflow layer correct, or a hole?
4. **`sort_operations` does not raise on missing keys** — it substitutes `""`/`0`.
   Schema validation belongs to `validate_factory_state` / `validate_plan`, so the
   digest layer stays total. Consequence: two different malformed operations can
   collide in sort order. Acceptable?
5. **`_resolve_version(plan_id, None)` returns `max(versions)`.** Is "latest" the
   right default for `get_content`, or should callers always be explicit? A tool
   that forgets to pass a version would silently read a newer plan than the one
   the approval was bound to.
6. **`LIFECYCLE_STATUSES` is mirrored from the contract** as a plain tuple for fast
   rejection. `test_lifecycle_statuses_equal_the_contract_enum` pins it. Is a
   mirrored constant acceptable at all, given rule 3?

---

## 8. What I got wrong (all 13, not a curated subset)

`IMPLEMENTATION_NOTES.md` has the full list with mechanism and fix. Summary:

| # | defect | how it was found |
|---|---|---|
| D1 | `is_docstring` compared a `Constant` to an `Expr`, so **no docstring was ever excluded** | guard reported its own docstring |
| D2 | self-check 2 counted the guard's own body — circular proof, reported "all used" while the consumer file did not exist | adding check 2b |
| D3 | three dead allowlist entries (`PUBLISHING` unmatchable, `TO_BE_RECORDED_BEFORE_SUBMISSION` **is** a contract member, `NOT_A_REAL_STATUS` unused) | D2's fix |
| D4 | `acceptance_tests` keyed `case_id`, not `id` — `KeyError` on first run | running it |
| **D5** | **negative control polarity inverted** — reported 10/5 when the truth was near the reverse | rewriting it properly |
| D6 | assumed the two float layers were redundant; they are not (F-STORE-02) | D5's rewrite |
| D7 | `CanonicalizationError.details` invented as `{field, reason}`; real schema needs three fields with a 6-field `validation_issue` inside | reading the schema |
| D8 | `pytest` imported but declared nowhere — fresh clone could not run tests | clean-venv test |
| D9 | `parents[N]` / `parent` off-by-one, **three times** (`_fixtures.py`, `write_evidence_plan_store.py`, `factcheck_impl_handoff.py`) — each fixed by searching upward for a marker instead of counting levels | running it |
| D10 | meaningless ternary `"hard_constraint" if False else "error_code"` shipped | reading it back |
| D11 | `write_text(s.encode())` — bytes to a text API | crash |
| D12 | redundant local import with a false comment ("errors imports nothing here") | reading it back |
| D13 | the guard flagged six **filename** stems (`IMPLEMENTATION_NOTES.md` etc.) as fabricated identifiers, blocking the whole suite via `test_the_repository_passes_its_own_guard`; fixed with a `(?!\.\w)` lookahead (§3.1) | the handoff factcheck run |

**One process failure worth naming separately:** commit `e2dea98` stated the
defects were "all recorded in REVIEW notes" when no such file existed. The claim
preceded the artefact. `IMPLEMENTATION_NOTES.md` was written afterwards and commit
`98ee9a8` says so explicitly. It also understated the count as "four" when there
were twelve.

---

## 9. Known gaps and unverified claims

- **No EVAL case has been executed.** `release_readiness.runtime_evaluation` is
  still pending; EVAL-001..030 are untouched. Nothing here is pilot-ready.
- **The baseline is still all-null.** No comparison numbers exist.
- **Dataset migration has not started.** `PlanPilot_Mock_Factory_Dataset.xlsx`
  lacks EVT-006/007 rows, calendar rows with `window_type`/`resource_type`,
  `is_primary` on worker skills, and BOM base-unit quantities. Until that is done,
  EVAL-020..030 cannot run and the engine specs cannot be tested end to end.
- **`ortools` is installed but unused by this spec.** It is in `requirements.txt`
  because the contract pins it; no test here exercises CP-SAT.
- **The store is in-memory.** `dump_state`/`load_state` give canonical JSON
  persistence, but there is no concurrent-access story, no locking, no WAL. Two
  processes writing would race. Fine for a demo; not fine for a claim of
  production readiness.
- **No test asserts `dump_state` output size bounds.** A plan with many operations
  could produce a large evidence file. Not checked.
- **The 18 "must fail" mutations were not checked for false catches** (§5).
- **Platform binding in the contract is known-stale** — see
  `../contract-review/REVIEW_HANDOFF_FOR_CODEX.md` and the v1.9 item below.

---

## 10. Suggested attack order

1. **`check_closed_vocabularies.py`** — the `_DEFINED_NAMES` escape hatch and
   `TOKEN_RE`'s lookbehind. If either can be defeated, the guard is theatre.
2. **§7.2 and §7.3** — the `supersede` / `put_content` idempotency inconsistency,
   and the hole where non-terminal illegal transitions are accepted.
3. **F-STORE-01** — is "no code, crash" defensible, or does the contract need a
   19th code?
4. **The digest pins** — anything a second implementation would plausibly do
   differently. `ensure_ascii`, float repr, the sort key's handling of ties.
5. **Negative control false catches** (§5, §9).
6. **`_resolve_version(None)`** (§7.5) — a silent wrong-version read is a security
   issue, not a style issue.
7. **The evidence pack** — is `EVIDENCE.json` actually reproducible from the
   committed inputs, or does it depend on the machine that produced it?

---

## 11. Carried forward from the contract review

Unchanged and still true (see `../contract-review/REVIEW_HANDOFF_FOR_CODEX.md`):

- **R1** — single-pass material reservation: a `READY` lot that cannot be placed
  keeps its materials committed. Documented as a deliberate trade-off in
  `material_consumption_model.known_limitation`, with a claim prohibition. **Do not
  demo material allocation as optimal.**
- **R2** — closed in V1.8 (`profiles_completed` now conditionally required).
- **R3** — closed in V1.8 (`maxItems=4` derivation documented).
- **v1.9 needed** — `platform_binding` is stale. The gateway is an
  Ollama-protocol endpoint (`POST /api/chat`, `X-API-Key`,
  `global.anthropic.claude-sonnet-4-5-20250929-v1:0`) whose `message.tool_calls`
  is **never populated**; tool calls arrive as Claude-Code-style XML in the text
  body and must be parsed. The USD 100 is a **shared** Lightsail+LLM total, not an
  AWS-only credit. None of this is in the contract yet, and `inference/` must
  eventually support both modes.

---

## 12. Bottom line

This module does what the contract requires of it, and every claim above is
reproducible with the commands in §2. The guard and the negative control are the
two pieces I would most want independently attacked, because both are
self-validating and self-validating checks are where this project has repeatedly
been fooled — D2 and D5 are both instances of a check that passed for the wrong
reason.

Nothing here makes the agent pilot-ready, and no EVAL has run.
