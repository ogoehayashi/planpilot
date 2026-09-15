# Formal runtime acceptance evidence

`python tools/run_evals.py` is a fail-closed gate, not a smoke-test runner. It
writes one result for every contract case, preserves the exact scenario and
pass condition, and exits with code 2 while any case is blocked.

The current artifact intentionally reports `0 PASS / 0 FAIL / 30 BLOCKED`.
There are no dedicated end-to-end case runners or reviewable per-case artifacts
yet. Unit tests and compact-fixture checks live under `smoke-harness` and cannot
promote an EVAL case.
