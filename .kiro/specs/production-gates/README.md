# `production-gates` — teammate entry point (G4)

Self-contained Design-First handoff for **G4: production entry, backup
verification/recovery, and deployment-boundary hardening** around the
review-closed G2/G3 publisher core.

Read in this order: `START_PROMPT.md` → `design.md` → `tasks.md`.

**Rev.3/4 note:** rev.2's fact base was contaminated by a different
PlanPilot data model (invented columns, tables, triggers, endpoints).
Design §0 is now a fact table where every line was personally re-read
from `855a5c0`, and §11 lists every struck invention so no stale phrase
survives in these four files. If you find a §0 line that doesn't match
the tree, STOP the phase and fix the design — never code against a bad
fact.

The authoritative requirements remain
`contract/planpilot_agent_contract_v1.8.json` (Claude Sonnet **4.5**
JSON API on Lightsail/Bedrock — never 4.6/Agent Runtime). Design-First:
no `requirements.md` by design.

## What this spec covers (final wording — matches design sections)

- **Phase 1 / design §2 (A):** `clock_session` defence in depth on the
  REAL 4-column schema — A1 fail-closed `_persist` (clock.py IS
  modified), A2 three RAISE-ABORT triggers
  (`no_delete` / `no_anchor_update` / `no_ts_rewind`) + startup
  trigger-shape digest guard, A3 raw-SQL tamper matrix; the
  drop-triggers sentinel test lives only in the negctl sandbox.
- **Phase 2 / design §3 (B):** one-shot env snapshot for startup-config
  fields (the documented exception, scoped by the Batch-A review: the
  Bedrock CREDENTIAL and network switch stay per-call re-read for key
  rotation, while region/model/daily limit belong to
  `BedrockClient.__init__`); pure
  `parse_startup_env` + separate filesystem `preflight`; closed
  `PLANPILOT_ENV` set (production refuses loopback binds — P1-2);
  full secret held (`repr=False`), summary logs non-sensitive config +
  paths, secret as present/length only (P2 wording).
  (No invented `CONFIG_REFUSING_START` code — §7 naming hygiene.)
- **Phase 3 / design §4 (C):** health three-way split with an EXPLICIT
  compat decision: `/health` stays exactly today's DB-touching
  `{"status":"ok","service":"planpilot"}` body for
  `test_requirement_delivery` and Docker back-compat; new `/health/live`
  (process-only), `/health/ready` (independent per-call connection on a
  `file:...?mode=rw` URI — a MISSING path is refused with zero phantom
  creation, Batch-A P1-1 — plus real `BEGIN IMMEDIATE` and an
  in-transaction write falsifier — reservation alone false-greens a
  read-only file, probed
  on Windows; one shared busy_timeout/connect budget; ROLLBACK;
  200/503; no clock write), `/health/deep` (authed — 403 per
  this codebase's convention — fixed-200 diagnostics that aggregate
  EVERY gate into `overall_ok` with dev/prod split, never a gate
  target itself).
- **Phase 4 / design §5 (D):** `verify_audit_connection` (pure; the
  head-repair `INSERT OR IGNORE` in `Database.__init__` must NOT run on
  backups) + `tools/verify_backup.py` (`mode=ro&immutable=1`, SHA
  unchanged after verify, negatives incl. missing head);
  `tools/restore_database.py` with intent-ledger marker
  (PREPARED→QUARANTINED→REPLACED→RECEIPTED, every op logged with
  per-file SHA; reopen = reconcile-from-filesystem, rev.4 P0-3), quarantine dir preserving
  original filenames, atomic receipt bound to backup SHA + audit head,
  hard-kill convergence tests; restore-recovery gate in
  `startup_config` refusing silent service (all envs) until an ack
  binds the exact backup SHA via a per-restore ack sidecar
  (`restore_database.py --ack`; no env-variable bypass — ack is voided
  by ANY new restore including a same-backup re-run, and by any receipt
  byte-edit; `restored_db_sha256_at_ack` is evidence, not a boot
  constraint). Rev.4/5: the diagnostics-only SERVER is
  DELETED — constructing `Database`/`ScenarioClock` on an unacked
  restored db mutates it (DDL, head-repair, triggers, clock attach);
  diagnostics are OFFLINE-only via a read-only CLI that imports no
  Server/Database class.
- **Phase 5 / design §6 (E):** deployment truth on the REAL files —
  `compose.yaml` (api service gets `backups:/backups:ro`), Dockerfile
  HEALTHCHECK **switches from `/health` to `/health/ready`** (the file
  already has an HTTP healthcheck today), wheelhouse manifest+generator
  committed while wheels stay OUT of Git; `tools/deploy_gate.py`
  pure-env logic + explicit I/O probes (Bedrock: EITHER
  `PLANPILOT_BEDROCK_API_KEY` or repo-external, non-symlink, mode-0600
  `PLANPILOT_BEDROCK_KEY_FILE`; production profile must match
  `PLANPILOT_ENV`); EVAL posture re-pinned to the REAL facts:
  `run_evals.py` frozen bytes, `cases=30 blocked=30 / exit 1`
  (measured), smoke harness isolation against the REAL
  `tests/evidence/runtime-eval` boundary (not `results/agent_eval`).
- **Phase 6:** negctl additions (ready→200, restore skips chain verify,
  marker/ack bypass, production secret bypass, no-op trigger accepted)
  with must-fail/hold counts locked after implementation, then the G3
  double-commit evidence discipline → deliverable bundle under
  `D:\PlanPilot_backups`.

**Formal EVAL stays BLOCKED** until a real AWS/Bedrock run; Docker
absence is `BLOCKED`, never a PASS. Nothing in this spec's completion
implies EVAL PASS.

## Repo anchors

- Development parent: `855a5c0` (G3 attestation seal; evidence body
  `4c26366`). Chain: `2e39fd1 → 5d2b656 → 7b6b238 → 1d14a1e → 4c26366 →
  855a5c0`.
- Baseline tag `g2-baseline-5bf299a` → `5bf299adb1e0…` — never move.
- G3 deliverable bundle: `D:\PlanPilot_backups\planpilot-build_g2-g3_855a5c0_20260923.bundle`,
  SHA-256 `91d90657…29692c` (`#`-comment sidecar; `sha256sum -c` clean).
- Contract SHA-256 `b92e53f4…fe639` — frozen.
- Inherited suite numbers (record fresh at Phase 0; never copy):
  targeted 92 / unit 742 / full 750 / negctl 8 / vocabulary PASS.
- tasks.md rev.5 has **35 checkboxes** (counted with
  `grep -c '^- \[ \]'` at write time; counts are NEVER carried from a
  previous revision — that habit is what produced the "36 vs 27"
  incident).
