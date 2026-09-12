# Review Prompt — hand this to an independent reviewer (Codex, Claude, or a human)

> Self-contained. The reviewer needs no prior context. Copy everything below the
> line into a fresh agent session pointed at the repo.

---

You are doing an **adversarial code review** of one module of a hackathon
production-planning agent. Your job is to find defects, not to confirm quality.
Two independent audits have already run. The first found 15 issues (F1–F15);
every one was reproduced against a clean tree and fixed or formally accepted.
The second found **two P0 defects the first missed** — the store never validated
content against the contract at all, and its optimistic-concurrency check was a
tautology. Both were reproduced, both are fixed, both are pinned by regression
tests and by negative-control mutations.

Read that as the bar, not as a clean bill of health. Two audits missing
different things is evidence that a third can still find more.

## Repo and how to run it

```
cd E:\PlanPilot-Hackathon\planpilot-build
set PY=E:\PlanPilot-Hackathon\contract-review\.venv\Scripts\python.exe
```

Python 3.11, venv already has `jsonschema 4.23.0`, `ortools 9.11.4210`,
`pytest 9.1.1`. **No network, no LLM, no API key, no dataset is needed or used** —
this module is pure deterministic Python. You should never have to install anything.

Verify the baseline before you start (all should pass; if any fails, stop and say so):

```
%PY% -m pytest tests/ -q                          # 337 passed
%PY% tools/check_closed_vocabularies.py           # CLOSED VOCABULARY CHECK | PASS
%PY% tools/factcheck_impl_handoff.py              # HANDOFF FACT-CHECK | fails=0
sha256sum contract/planpilot_agent_contract_v1.8.json   # b92e53f4ff054105...
```

## What the module is

`src/planpilot/store/` — the plan store and canonical digest. Five files:
`digest.py`, `errors.py`, `plan_store.py`, `persistence_schema.py`, `__init__.py`
(1,609 lines).

`src/planpilot/validation/` — the production contract validator the store now
calls at every write boundary (410 lines, added to fix P0-1). It is the single
validation entry point for both `src/` and `tests/`; `tests/_fixtures.py` is no
longer on any production path.

It is the foundation every later module depends on. Its whole reason to exist is
one property: **a plan's digest is a pure function of its content**, so a
fabricated or corrupted plan (the known failure mode of the LLM gateway this agent
will sit behind) cannot survive into approval or publish.

The contract it implements is `contract/planpilot_agent_contract_v1.8.json`
(189,921 bytes). The relevant sections are `plan_store`, `$defs.plan_content`,
`$defs.plan_lifecycle`, `$defs.error_details_*`, and
`scheduling_engine.determinism.canonical_serialization`.

## Read in this order

1. `REVIEW_HANDOFF_IMPLEMENTATION.md` — the implementer's own sceptical handoff,
   including §2.2 (the prior audit) and §10 (suggested attack order)
2. `IMPLEMENTATION_NOTES.md` — self-found defects (D-series), the F1–F15 audit
   disposition table, the P0-1/P0-2 findings from the second audit, and design
   decisions
3. `.kiro/specs/plan-store-and-digest/design.md` and `tasks.md`
4. the code itself

## Where to attack (highest value first)

1. **The digest determinism claim.** `canonical_plan_digest` must be a pure
   function of content. Hunt for any input where two logically-equal contents
   hash differently, or one content hashes differently across runs/machines.
   `sort_operations` was already found order-dependent (F4) and non-total (F12) —
   both "fixed". Check whether the fixes are complete, or whether a third ordering
   hole remains (e.g. the `_total_key` type-rank tiebreak, float repr, dict
   ordering inside an operation, the `ensure_ascii=False` choice).
2. **The closed-vocabulary guard** (`tools/check_closed_vocabularies.py`). This is
   the implementer's proudest piece and the most likely to be theatre. It scans
   `src/ tests/ tools/` for string constants that look like contract identifiers
   but are not members of a closed vocabulary. An independent attack already found
   **ten bypasses**, three of which were "fixed" and seven accepted as limits
   (§3.2 of the handoff). Try to find an eleventh, or show a "fixed" one still
   works. The `_DEFINED_NAMES` escape hatch and the `TOKEN_RE` lookarounds are the
   weak points.
3. **Error-details schema conformance.** Every `StoreError.details` must satisfy
   its `$defs.error_details_*` schema with `additionalProperties: false`. F2 and F5
   were exactly this class of bug and slipped through because the test that should
   have caught them (`test_errors_schema.py`) was **cited in two files but never
   written**. Check that the now-written version actually covers every error path,
   and look for a *second* ghost test — any test referenced in prose/comments that
   does not exist, or any assertion that cannot fail.
4. **The negative control** (`tests/negative_control/test_plan_store_negctl.py`).
   It mutates the source 19 ways and asserts the suite catches 18 (one is a
   deliberate defence-in-depth case). Verify it is not theatre: do the mutations
   actually apply? Could a mutation be "caught" for the wrong reason? It once
   inverted its own polarity (D5) and once asserted git-cleanliness instead of
   byte-restoration — both fixed; check the fixes hold.
5. **`load_state` / `dump_state` round-trip.** F1 was here. Try to construct a
   dumped state that loads but leaves the store internally inconsistent
   (lifecycle pointing at wrong content, terminal status that can be revived,
   superseded events that double-fire).
6. **The deferred findings.** F7 (back-transitions accepted), F9 (raw internal
   dicts handed out), F10 (fields cannot be cleared) were accepted, not fixed.
   Argue whether any is actually a real defect for this module's contract, or
   whether accepting it was correct.

## What "done" means for your review

Report each finding as: **severity, file:line, the exact reproduction, and whether
it contradicts a claim in the docs.** Reproduce against the current tree before
reporting — do not trust the handoff's self-assessment. If you find nothing in an
area, say what you tried, because "I could not break X after trying Y" is itself
useful evidence.

## Honest scope limits (do not report these as defects)

- **No EVAL has run.** `release_readiness.runtime_evaluation` is pending. This is
  unit evidence for one module, not a pilot-readiness claim.
- **The baseline is all-null** — no comparison numbers exist yet.
- **The store is in-memory** with canonical-JSON persistence but no locking, WAL,
  or concurrent-access story. Two processes writing would race. Known and accepted
  for a demo.
- **`ortools` is installed but unused by this spec** — it is pinned because the
  contract requires it; no test here exercises CP-SAT.
- **14 of the 15 specs listed in `.kiro/specs/README.md` do not exist yet** —
  there is no spec directory for them, let alone code. No scheduler, approval
  service, audit chain, tool layer, inference client, dataset migration, or UI.
  This module is the foundation, not the agent. (An earlier version of this
  document said "8 of 15", which was unsupported by anything on disk.)
