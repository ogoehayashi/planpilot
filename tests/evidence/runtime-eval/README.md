# Formal EVAL readiness — BLOCKED

Run `python tools/run_evals.py` from the project root. Expected exit code: **1**.
Current status: **0 PASS / 0 FAIL / 30 BLOCKED**. No formal scenario was executed.

Every case preserves its contract scenario and complete pass condition. The
inventory lists missing executors and evidence: inputs, outputs, traces,
canonical digest or verified no-plan outcome, timings, approvals or verified
non-applicability, and audit hash-chain/failure-atomicity verification.
A present workbook or passing compact assertion cannot establish acceptance.
No evidence-upload or automatic promotion path exists yet.

`EVIDENCE.json` replaces the invalid old green artifact. Its timestamp records
inventory generation, not execution of the blocked cases. Prior evidence is
explicitly retracted in `../retracted/`; never include it in acceptance totals.
Compact checks use `python tools/run_smoke_harness.py` and write separately to
`../compact-smoke/SMOKE_EVIDENCE.json`, without formal case identifiers.
