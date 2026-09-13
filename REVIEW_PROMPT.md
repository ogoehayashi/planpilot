# Review Prompt — hand this to an independent reviewer (Codex, Claude, or a human)

> Self-contained. The reviewer needs no prior context. Copy everything below the
> line into a fresh agent session pointed at the repo.

---

You are doing an **adversarial code review** of one module of a hackathon
production-planning agent. Your job is to find defects, not to confirm quality.
**Six** independent audits have already run, and each found things the one
before it missed — after that round's docs had already claimed the module was
sound:

1. The first found 15 issues (F1–F15); each reproduced against a clean tree and
   fixed or formally accepted.
2. The second found **two P0 defects the first missed**: the store never
   validated content against the contract at all, and its optimistic-concurrency
   check was a tautology.
3. The third found **an authority bypass and three persistence gaps the second
   missed**: `create_lifecycle()` could write a `PUBLISHED` record straight
   past the version gate; `put_content()` could add a version without retiring
   the previous one; `load_state()` did not relate supersede events to the
   lifecycle records they invalidate; and contract selection was lexicographic
   (v1.9 beat v1.10) with an unverified path override.
4. The fourth found a destructive/non-durable supersede handoff, two event
   forgery routes, a persistence version-gap bypass, stale versions reacquiring
   approval authority, direct publish without approval, replaceable approval
   bindings, and non-terminal publication.
5. The fifth found that public `transition(..., "SUPERSEDED")` bypassed event
   creation, so the store could dump a terminal state that its own `load_state()`
   permanently refused.
6. The sixth found that killing the negative-control process could leave a
   mutation in the real working source, that isolated UTF-16 surrogates leaked a
   raw codec exception, and that `-0.0` and `0.0` produced different digests.

Every actionable fourth-round finding was reproduced against the clean
`fc908d7` baseline before being fixed. Its defences are pinned by committed
regression tests and source mutations.

Read that as the bar, not as a clean bill of health. Six audits finding
different things is evidence that another review can still find more.

## Repo and how to run it

From a clone, create or activate any Python 3.11 virtual environment, then run:

```
python -m pip install -r requirements.txt -r requirements-dev.txt
```

The pinned environment uses `jsonschema 4.23.0`, `ortools 9.11.4210` and
`pytest 9.1.1`. No LLM, API key or dataset is needed or used. Network is needed
only if those pinned wheels are not already cached locally.

Verify the baseline before you start (all should pass; if any fails, stop and say so):

```
python -m pytest tests/ -q                         # 421 passed
python tools/check_closed_vocabularies.py          # CLOSED VOCABULARY CHECK | PASS
python tools/factcheck_impl_handoff.py             # HANDOFF FACT-CHECK | fails=0
python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('contract/planpilot_agent_contract_v1.8.json').read_bytes()).hexdigest())"
```

## What the module is

`src/planpilot/store/` — the plan store and canonical digest. Five files:
`digest.py`, `errors.py`, `plan_store.py`, `persistence_schema.py`, `__init__.py`
(2,163 lines).

`src/planpilot/validation/` — the production contract validator the store now
calls at every write boundary (559 lines, added to fix P0-1). It is the single
validation entry point for both `src/` and `tests/`; `tests/_fixtures.py` is no
longer on any production path.

It is the foundation every later module depends on. It combines two separate
properties: **a plan's digest is a pure function of its content**, and every
write is contract-schema validated. The digest detects post-signing corruption;
schema validation—not the digest—rejects a re-signed fabrication.

The contract it implements is `contract/planpilot_agent_contract_v1.8.json`
(189,921 bytes). The relevant sections are `plan_store`, `$defs.plan_content`,
`$defs.plan_lifecycle`, `$defs.error_details_*`, and
`scheduling_engine.determinism.canonical_serialization`.

## Read in this order

1. `SIXTH_AUDIT_REMEDIATION_REPORT.md` — current changes and readiness verdict
2. `REVIEW_HANDOFF_IMPLEMENTATION.md` — the implementer's own sceptical handoff,
   including §2.2–§2.7 (the six audits) and §10 (suggested attack order)
3. `IMPLEMENTATION_NOTES.md` — self-found defects (D1–D21), the F1–F15 audit
   disposition table, the P0-1/P0-2 findings from the second audit, the
   P0-bis/P1-a/P1-b/P2 findings from the third, and design decisions
4. `.kiro/specs/plan-store-and-digest/design.md` and `tasks.md`
5. the code itself

## Where to attack (highest value first)

1. **The digest determinism claim.** `canonical_plan_digest` must be a pure
   function of content. Hunt for any input where two logically-equal contents
   hash differently, or one content hashes differently across runs/machines.
   `sort_operations` was already found order-dependent (F4) and non-total (F12) —
   both "fixed". Check whether the fixes are complete, or whether a third ordering
   hole remains (e.g. the `_total_key` type-rank tiebreak, float repr beyond
   signed zero, dict ordering inside an operation, the `ensure_ascii=False`
   choice, or a Unicode edge not covered by the surrogate guard).
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
   It mutates a disposable repository copy 58 ways across five files (`digest.py`,
   `plan_store.py`, `errors.py`, `src/planpilot/validation/schema.py`,
   `tests/_fixtures.py`) and asserts the suite catches 56; the other 2 are
   deliberate defence-in-depth cases
   where removing ONE layer must leave the suite green because another still holds.
   Verify it is not theatre: do the mutations actually apply (each uses
   `_sub_once`, which fails loudly on a non-unique or absent anchor)? Could a
   mutation be "caught" for the wrong reason — and note that TWICE now the control
   has caught exactly that in a fix the implementer had just written (§2.3 and
   §2.4: a test that passed for the wrong reason, so removing the defence left
   the suite green). It once inverted its own polarity (D5) and once asserted
   git-cleanliness instead of byte-restoration — both fixed. A sixth review then
   proved `finally` could not protect real files from process death, so mutations
   now run only in a temp copy; verify the child really imports from that copy and
   cannot write through to the working repository.
5. **The supersede outbox.** Crash the conceptual consumer before and after its
   external side effect and before acknowledgement. Attack event/ack identity,
   duplicate and orphan records, timestamp ordering, backwards compatibility,
   defensive copying and deterministic dump order. The approval service is not
   built, so verify the store API makes an idempotent consumer possible without
   mistaking that for end-to-end exactly-once.
   Also prove that public `transition()` cannot target SUPERSEDED and that every
   supported route into that status creates exactly one immutable event.
6. **`load_state` / `dump_state` round-trip.** F1, P1-a, P1-b and the fourth audit
   all landed here,
   so this is the most-worked surface and the likeliest place a constraint is
   either incomplete or over-strict. Try to construct a dumped state that loads
   but leaves the store internally inconsistent (lifecycle pointing at wrong
   content, terminal status revived, superseded events that double-fire, a stale
   version holding APPROVED/PUBLISHED). Then try the opposite: find a state the
   LIVE API can produce that `load_state` now WRONGLY refuses — the layer-3
   authority check was once too strict and broke round-trip fidelity for states
   the then-current API allowed (D15), so a second over-refusal is plausible. Also check
   `dump_state` really is order-independent now that it sorts `superseded_events`.
7. **The remaining deferred finding.** F7 (published back-transition) and F9
   (raw event dicts) are now fixed. F10—fields cannot be cleared—remains accepted.
   Determine whether clearing is actually required by any contract path.

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
