# Design — `production-gates` (G4) rev.5

**Contract:** `contract/planpilot_agent_contract_v1.8.json`
(SHA-256 `b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639`,
frozen — this spec never edits it)
**Tasks:** `./tasks.md` · **Start prompt:** `./START_PROMPT.md`

rev.2 is RETRACTED as fact base: large parts of its §0 described a
DIFFERENT PlanPilot data model (invented `host/pid/txn_id/boot_id/version`
columns, non-existent `db.clock`/`clock_history`/`digest_history`,
wrong auth/health/backup/eval/docker/contract claims). rev.3 rebuilds
§0 exclusively from `git show 855a5c0` working-tree source read line by
line on 2026-09-23, plus LIVE PROBES marked [LP]. If rev.3 and the code
ever disagree, THE CODE WINS and this design is stale — Phase 0 exists
to catch exactly that.

---

## §0 Fact table (verified against `855a5c0`, each line personally read)

### F1 — clock layer (`src/planpilot/clock.py`, 277 lines)
- Clock kinds: `WallClock` (fail-safe default, SGT +08:00), `FixedClock`
  (tests/replay), `ScenarioClock` (demo; anchored, advances with real
  elapsed time, restart-monotonic via a durable session).
- `clock_from_env(environ, host)` (clock.py:220): `PLANPILOT_CLOCK_MODE`
  unset/`wall` → Wall; `scenario` → ScenarioClock, refuses non-loopback
  bind unless `PLANPILOT_ALLOW_PUBLIC_SCENARIO=1`, requires explicit
  `PLANPILOT_SCENARIO_NOW` with `+08:00` offset; unknown values RAISE
  (startup refused). **Already fail-safe; G4 does not touch this policy.**
- Scenario session state lives in the ONE `clock_session` table (see F2)
  plus in-memory `self._base/_boot`. There is NO `db.clock` table, NO
  `clock_history`, NO `clock_service_session`, NO host/pid/txn_id/boot_id
  (rev.2 inventions — all struck).
- `attach_database` (clock.py:139-176): first start INSERTs
  `(1, anchor, wall_now, anchor)`; reopen checks stored anchor equality
  (foreign anchor → ValueError fail-closed), resumes
  `max(anchor + real_downtime, last_issued_scenario_time)`.
- **The real defect (A1 target)** — `_persist` (clock.py:188-208): the
  high-water `UPDATE … CASE … RETURNING` returns `row is None` when the
  `clock_session` row was deleted out from under the clock; line 205-206
  then `return stamp` — a stamp that was NEVER persisted. Callers keep a
  live-but-unanchored time; the next attach cannot see it. Fix must
  modify `clock.py` (rev.2's "clock.py unchanged" was false): row
  missing → raise (fail-closed), never silently stamp.

### F2 — schema (`src/planpilot/persistence.py`, 231 lines)
- Tables (executescript at init, lines 66-129): `authority_state`,
  `audit_chain(id, record, previous_hash, event_hash)`,
  `audit_chain_head(singleton, entry_count, event_hash)`,
  **`clock_session(singleton, scenario_anchor, real_wall_started_at,
  last_issued_scenario_time)`** (persistence.py:85-90 — these are the
  ONLY three payload columns), `publication_receipt`,
  `idempotency_registry`.
- Triggers that EXIST: `audit_chain_no_update` (:113),
  `audit_chain_no_delete` (:117), `audit_chain_advance_head` (:121).
  `security_events_no_update/_no_delete`, `decision_traces_no_update/
  _no_delete` exist in `src/planpilot/audit.py:61-76`. There is NO
  `digest_history` table and NO `approval_records_source_key_check`
  trigger (rev.2 inventions). `factory_states` has no triggers
  (factory_state.py:408).
- **`clock_session` today has ZERO SQL guards** (grep of every
  `CREATE TRIGGER` in persistence.py:113/:117/:121 — only the three
  `audit_chain_*` ones exist). That is the real G4-A gap: DELETE kills
  the anchor row silently — and after such a delete `_persist`
  fail-OPENs (see A1); UPDATE of anchor/wall-start corrupts resume math
  while the rewind-CASE keeps `last_issued_scenario_time` monotonic
  under honest writes only — raw SQL can also rewind it directly.
- `Database.__init__` opens with `PRAGMA journal_mode=WAL`,
  `synchronous=FULL`, `foreign_keys=ON` (:63-65), then
  `INSERT OR IGNORE INTO audit_chain_head` (:134-137) — the head
  self-repair happens HERE, not in verify_audit.
- `verify_audit()` (:218-231) is READ-ONLY: walks `audit_chain`, checks
  id contiguity / previous_hash / re-SHA of `(previous+record)`, then
  compares `audit_chain_head` — returns bool. Rev.2's "verify_audit
  repairs" was wrong. Its defect for G4: it is bound to a `Database`
  instance (whose constructor mutates files and repairs head), so there
  is NO way to verify a static file without opening it writable first.
- `transaction()` (:176-185): `BEGIN IMMEDIATE` under `self.lock`
  (RLock). `close()` (:187). [LP-1 CORRECTED in rev.4] my rev.3 claim
  "a clean close leaves -wal behind" did NOT reproduce: reviewer run and
  my own re-probe at `855a5c0` both show a temp dir containing ONLY
  `probe.db` after open→clean-close (WAL checkpointed on close);
  `-wal/-shm` exist only WHILE a transaction is open. Restated rule:
  WAL/SHM may be present after crash, with live connections, or before
  checkpoint — so restore treats main/-wal/-shm as an atomic trio
  UNCONDITIONALLY; no phase may assume the sidecars are absent.

### F3 — auth & HTTP surface (`tools/api_server.py`, 425 lines)
- `Server.__init__` (:76-78): RAISES unless secret is present and
  ≥ 32 chars. Rev.2's "empty secret silently allows" was FALSE; there is
  no anonymous mode to fix. The real config gap is different (F6):
  `main()` reads each env var ad hoc, there is no `PLANPILOT_ENV`, no
  production profile, no one-shot snapshot, startup values can drift
  from what the handler layer later re-reads.
- `security.authenticate(token, secret, permission)`
  (src/planpilot/security.py:12): HMAC token, role→permission sets
  (`planner: plan/approve_publish/approve_secondary`,
  `manager: plan/approve_overtime/approve_due_date`). Route convention:
  **`PermissionError` → HTTP 403** (GET :243-244, POST :416-417). 401 is
  NOT this codebase's convention — deep diagnostics must fail with 403
  too.
- GET routes (:168-238): `/health` — **DB-touching** (`db.lock` +
  `SELECT 1`), returns `{"status":"ok","service":"planpilot"}`, NO auth
  — rev.2's "pure liveness already" was FALSE. `/clock` (clock.status():
  kind/now/scenario/uptime_seconds, session sub-dict when attached — no
  "version"/"source" fields). `/metrics`, `/plans`, `/approval/status`,
  `/audit/status` (verify_audit + head + recent 12), `/`,
  `/web/p1_3.js`. POST routes (:270-300): `/plan /schedule
  /approval/request /approval/decide /publish /agent/chat` — `publish`
  requires `approve_publish` via middleware route; bulk POST auth is
  `plan`. There is NO `/api/v1/publish` (rev.2 invention).
- Compat consumers of `/health`: `tests/unit/test_requirement_delivery.py:157`
  asserts `("/health")[0] == 200`; Dockerfile HEALTHCHECK hits
  `http://127.0.0.1:8080/health`; no web/ UI consumer found. G4 may NOT
  silently redefine `/health` — see §4 split with explicit compat
  decision.

### F4 — backup / restore / eval today
- `src/planpilot/backup.py` `backup_database(source, destination) -> None`
  ([LP] returns None — rev.2's dict return was invented). Online
  `sqlite3.backup` from a `mode=ro` URI, `PRAGMA integrity_check` on the
  copy, fsync, atomic `os.replace` to destination. No restore, no
  verification tool, no receipt.
- `tools/backup_database.py`: one-liner CLI, prints nothing, exits 0.
- `tools/scheduled_backup.py`: run(source, dir, keep=14) → timestamped
  `.db` + retention prune, returns dict `{'backup','removed'}`; there is
  NO status.json (rev.2 invention); failures today propagate as
  exceptions with no artifact.
- `tools/run_evals.py`: fail-closed inventory; OUT =
  `tests/evidence/runtime-eval` (tracked dir exists); [LP] real run (stdout, run_evals.py:72-73):
  `formal EVAL readiness evidence: <ABS path>` — line 1's path is
  assembled from ROOT (:17), so acceptance asserts ROOT-prefix +
  `tests/evidence/runtime-eval`-suffix, never a fixed relative string —
  then `cases=30 passed=0 failed=0 blocked=30` (exact), **exit 1**
  (rev.2's exit 2 was wrong; rev.4's `BLOCKED:` line never existed). Contract has exactly 30 `EVAL-*` ids (grep-verified).
  `results/agent_eval` does not exist (invention).
- `tools/run_smoke_harness.py`: already a SEPARATE script with its own
  OUT `tests/evidence/compact-smoke`, and a guard rejecting output under
  the formal runtime-eval dir (:82-84). EVAL/smoke isolation is partly
  real already; G4 pins it by test, not re-invention.
- Deployment files: **`compose.yaml`** (NOT docker-compose.yml): api
  service (build ., ports 127.0.0.1:8080, read_only, tmpfs /tmp, cap_drop
  ALL, no-new-privileges, backups volume NOT mounted on api — only the
  separate `backup` operations-profile service sees /backups), plus
  `backup` service running scheduled_backup.py. Dockerfile
  (`python:3.11-slim`) pip-installs `requirements.txt` +
  `requirements-import.txt` **from PyPI at build time** (ortools
  9.11.4210 + jsonschema 4.23.0 — binary wheels; committing them to Git
  would bloat the repo/bundle by hundreds of MB — see §5 wheelhouse
  decision), runs as uid 10001, ENV `PLANPILOT_CLOCK_MODE=wall`,
  `PLANPILOT_FORBID_LLM_NETWORK=1`, HEALTHCHECK urllib GET `/health`
  30s/5s/start 15s.
- Bedrock client (`src/planpilot/inference/bedrock_client.py:99-116`):
  credential = ctor token, else `PLANPILOT_BEDROCK_KEY_FILE` (file read),
  else `PLANPILOT_BEDROCK_API_KEY`; single-line ASCII ≤16384 enforced.
  `PLANPILOT_FORBID_LLM_NETWORK` default '1' blocks `converse()`;
  `status()` reports configured/region/model/network_enabled. Contract
  `platform_binding`: **AWS Lightsail + Bedrock Claude Sonnet 4.5 JSON
  API** (contract :4513/:4532; Dockerfile default model id
  `global.anthropic.claude-sonnet-4-5-20250929-v1:0`) — rev.2's
  "Sonnet 4.6 Agent Runtime" was invented.

### F5 — audit chain shape (for the read-only verifier)
- Row hash (persistence.py:224, VERBATIM):
  `sha256((previous_hash + row["record"]).encode("utf-8"))` where
  `record` is the STORED STRING — `canonical()` (persistence.py:18-22,
  sort_keys/separators/ensure_ascii=False) ran at APPEND time
  (audit.py:209-210 `self.expected_head + canonical(self.record)`).
  Genesis previous = `"0"*64`; `AUD-%012d` ids; head row
  `(1, entry_count, event_hash)` maintained by `audit_chain_advance_head`.
  Chain tables + head live in THE SAME db file as everything else.
  **Verifier rule (P1-2):** the verifier MUST hash the stored record
  bytes as-is and MUST NOT re-canonicalize a parsed copy — re-canonical
  would silently ACCEPT whitespace mutation inside the JSON string
  (parse→re-dump normalizes it away). Same for `previous_hash` equality
  — string compare, no normalization.

### F6 — config gaps that ARE real
- No `PLANPILOT_ENV` anywhere; "production" is only implied by env
  hygiene. `main()` reads `PLANPILOT_HOST/PORT/DB/FACTORY_ROOT/
  AUTH_SECRET` one by one (:404-415) — no one-shot snapshot, no
  validation preflight (e.g. factory_root existence), host/port honored
  inconsistently vs handler-time re-reads of Bedrock env vars per call
  (bedrock_client reads `os.environ` at request time, not boot).
- `Database(path)` with a nonexistent parent dir fails with a raw
  sqlite3 error, not an operator-readable refusal.

### F7 — G2/G3 inherited (unchanged from rev.2, correct there)
- HEAD anchor `855a5c0` (G3 attestation; evidence body `4c26366`);
  baseline tag `g2-baseline-5bf299a^{}` = `5bf299adb1e0…`; bundle kept
  out-of-repo at `D:\PlanPilot_backups\…855a5c0…bundle` (+.sha256).
- G3 discipline inherited: evidence-body commit first, verify against
  final Git blobs + no-.git harness copy + bundle→temp-clone, THEN a
  small attestation commit; Docker/tooling absence is `BLOCKED`, never
  a silent PASS; devlog is append-only.

---

## §1 Scope & hard boundaries

IN: clock_session SQL guards + fail-closed `_persist`; startup config
snapshot + preflight; health split (live/ready/deep) without breaking
the one real `/health` consumer; read-only audit verifier + backup
verification + crash-safe restore state machine with recovery gating;
deployment gate (compose.yaml/Dockerfile real files, wheelhouse
manifest, Bedrock credential preflight); EVAL truth locks; negctl
mutations; evidence pack per G3 discipline.

OUT (red lines): `contract/**` bytes (SHA `b92e53f4…fe639`); publisher
transaction core (`publisher.py`, G2 §3-5 semantics: `_probe_in_open_
transaction`, `Database.lock + BEGIN IMMEDIATE`, one transaction →
DURABLE_COMMITTED); approval/idempotency business semantics; the G3
delivered bundle; formal EVAL runner behavior (zero changes to
run_evals.py); real AWS/Bedrock execution (stays BLOCKED until a live
environment run happens — no G4 task "unblocks" EVAL).

No new pip deps. New error/wording strings must survive
`tools/check_closed_vocabularies.py` (it is a tripwire over KNOWN
entry points — honest scope per P3 lesson; run it after any wording
edit).

## §2 Phase 1 — clock_session defence in depth (A, rebuilt on real schema)

**A1 fail-closed `_persist`** (clock.py:188-208; the fail-open pair is
:205-206): row missing → `raise RuntimeError("clock session row
missing…")` instead of returning the un-persisted stamp. Honest flow
unaffected: `attach_database` (:151-160) INSERTs the singleton BEFORE
`self._db` is bound (:175), so no honest code path ever calls `_persist`
against a missing row — which is exactly what makes the raise safe.
Test: negctl sandbox (no triggers installed) `DELETE FROM
clock_session` then `clock.now()` raises; after restoring the row,
resumes correctly.

**A2 triggers on the THREE real columns** (persistence.py executescript,
`IF NOT EXISTS`, after :90):
1. `clock_session_no_delete` BEFORE DELETE → RAISE ABORT
   ('clock_session is anchored; delete is forbidden').
2. `clock_session_anchor_immutable` BEFORE UPDATE OF scenario_anchor,
   real_wall_started_at → RAISE ABORT (both are resume inputs; a
   legitimate use case for editing them does not exist — restart
   resume must trust them).
3. `clock_session_no_rewind` BEFORE UPDATE OF last_issued_scenario_time
   WHEN NEW.last_issued_scenario_time < OLD.last_issued_scenario_time →
   RAISE ABORT (high-water only; forward UPDATE of this column is the
   normal `_persist` path and MUST stay allowed).
INSERT of the singleton row by `attach_database` is the only write that
creates; no guard needed beyond the CHECK(singleton=1) already present.

**Trigger-shape guard (anti no-op shell):** `Database.__init__` runs the
canonical `CREATE TRIGGER IF NOT EXISTS` bodies FIRST, then reads
`sqlite_master.sql` for the three names and compares
per-line-normalised sha256 digests (whitespace and ONE optional
trailing semicolon only; NO textual token stripping anywhere — a global
`IF NOT EXISTS` regex once collided two bodies that differed only
inside a RAISE string). Honest final semantics (1.0.1-#1):
- a defence trigger MISSING BEFORE LAUNCH (pre-G4 resume file) is NOT
  rejected — `IF NOT EXISTS` installs the canonical body and the open
  proceeds (self-heal; rejecting that would brick honest upgrades, and
  the guard could never observe the pre-install state anyway);
- a SAME-NAME, DIFFERENT-BODY trigger (hollow pre-plant, or a trigger
  whose body SQLite rewrote under DDL) makes the stored fingerprint
  diverge => `RuntimeError` refusing construction;
- an out-of-band raw file editor dropping trigger AND rows is a known
  privileged limitation (that privilege already owns the file); reopen
  reinstalls the canonical defence.
Pre-creating a same-named no-op trigger therefore fails startup closed,
and `ALTER TABLE ... RENAME COLUMN` on clock_session — DDL that BEFORE
triggers never see — fails closed at the NEXT open, because SQLite
rewrites the stored trigger SQL and the digest stops matching (pinned
by tests; the refused open releases its handle so the file is
immediately rename-/deletable even on Windows).

**A3 tamper matrix (live DB, raw SQL, bypassing nothing):**
(RAISE(ABORT) surfaces through python sqlite3 as `IntegrityError` —
empirically pinned, NOT `OperationalError`.)
- DELETE row => IntegrityError (trigger 1).
- UPDATE scenario_anchor / real_wall_started_at => IntegrityError
  (trigger 2).
- UPDATE last_issued backwards => IntegrityError (trigger 3);
  forward UPDATE succeeds (regression pin for the legit `_persist`).
- Existing cross-restart invariants from G1.0.2 (anchor mismatch →
  ValueError; monotonic resume) re-run unchanged.

**A4 sentinel — where the "delete triggers then tests must fail" check
lives:** NOT in the normal pytest suite (it would intentionally redd
CI). It goes in `tests/negative_control/` as a sandbox/subprocess
mutation exactly like existing negctl rows: copy db source to a temp
workspace, strip the three `CREATE TRIGGER … clock_session …` blocks,
run a pinned subset (clock/persistence/publisher-file tests), assert
must-fail non-empty. Also a negctl row: pre-create no-op trigger →
expect Database construction fails (mutation flips the shape-guard to
pass-through).

## §3 Phase 2 — startup config: one snapshot, two layers

`parse_startup_env(env_snapshot) -> tuple[ParsedConfig | None, list[Issue]]`
is PURE (dict in, no I/O) and `preflight(parsed) -> list[Issue]` does ALL
filesystem checks in one place; `main()` reads `dict(os.environ)` ONCE,
feeds the snapshot to both, then constructs only from the parsed object
(no second env read for startup fields). Details:

- `PLANPILOT_ENV` closed to `development|production` — unknown value
  refuses (StartupConfigError, internal class, never a contract code).
  production additionally REFUSES loopback binds — STRUCTURAL check
  (review-3 #2: `ipaddress.is_loopback` incl. IPv4-mapped forms, plus
  a casefolded trailing-dot-stripped `localhost` hostname test; the
  old exact-tuple match was bypassable) — Batch-A P1-2: tasks 2.4
  promised it and the old parse accepted it; Phase 5 compose binds
  0.0.0.0 explicitly. development keeps the loopback default.
- secret: FULL VALUE held on the dataclass via
  `field(repr=False, compare=False)` — stdlib dataclasses only, NO
  pydantic `SecretStr` (no new deps; rev.3's SecretStr wording was
  wrong for this repo). ≥32 chars enforced in parse; `summary()`
  exposes NON-SENSITIVE config values and paths (env/host/port/the
  three paths/clock mode/timeout) plus present/length for the secret —
  the secret VALUE never appears (Batch-A P2 review: the old
  "booleans/counts only" claim was false; host/port/paths were logged).
- port/host/db/factory_root/backup_dir string fields;
  `ready_timeout_ms` default 1500; clock mode passthrough.
- preflight: factory_root itself must be a directory (not just parent);
  db dir creatable/writable; backup dir creatable if configured;
  port bindable.
- **Bedrock exception (intentional, documented; claim SCOPED by the
  Batch-A review — rev.5b's blanket "six vars stay PER-CALL" was not
  what the code does):** the CREDENTIAL (`PLANPILOT_BEDROCK_API_KEY` /
    `PLANPILOT_BEDROCK_KEY_FILE`, resolved inside `_credential()`,
  bedrock_client.py:109) and the NETWORK SWITCH
  (`PLANPILOT_FORBID_LLM_NETWORK`, read in `status()`/`converse()`,
  :124/:127) ARE per-call — key rotation needs no restart. `REGION` /
  `MODEL` / `DAILY_TOKENS` are read ONCE in `BedrockClient.__init__`
  (bedrock_client.py:78-90): runtime rotation of THOSE requires a
  process restart, which is fine because they are not StartupConfig
  fields and the snapshot rule does not cover them. The rotation test
  constructs a REAL client and flips the env (test_startup_config),
  never a source-text grep (guards remain against a well-meaning
  future "snapshot everything" refactor).
- `Server.__init__` secret check (api_server.py:72-73) stays byte-for-byte:
  in EVERY environment a missing/short secret is refused — that behavior
  is what dev relies on too ("dev with a valid secret boots"; rev.3's
  "dev boots" shorthand is corrected). compose.yaml + Dockerfile gain
  explicit `PLANPILOT_ENV=production`; start_local.ps1 stays development.

## §4 Phase 3 — health: honest split, no fake readiness

Consumer compatibility decision (explicit, not silent): `/health` KEEPS
today's exact semantics and body `{"status":"ok","service":
"planpilot"}` + `SELECT 1` under db.lock (test_requirement_delivery
stays green unchanged). New paths:

- `GET /health/live` — process-only, zero DB, 200 with
  `{"status":"ok","service":"planpilot","probe":"live"}`. Fields:
  existing `status/service` preserved, additions enumerated: `probe`.
- `GET /health/ready` — a writability probe, NOT a read probe. Its
  honest claim (round-4 wording): the probe proves the DB OPENS RW and
  a WRITER RESERVATION is acquirable+releasable. It is NOT a full
  durability or free-disk-space proof; do not oversell it in docs or
  error text.
  [REV.3→4 P0-1] rev.3's `mode=ro` connection was falsified by the
  reviewer: an ro connection can win a reserved lock yet still cannot
  write (`attempt to write a readonly database`). Correct protocol:
  independent PER-CALL connection opened `file:<resolved>?mode=rw` via
  URI — Batch-A P1-1 review falsified plain `sqlite3.connect(path)`:
  it silently CREATES a zero-byte file when the path vanished, the
  probe then succeeds and readiness false-greens a phantom DB.
  `mode=rw` refuses a missing path (SQLITE_CANTOPEN → 503, zero file
  created — pinned by test and by negctl mutation #6) and behaves like
  the default RW open on an existing file. Single budget
  (`ready_timeout_ms`, default 1500, ALSO the connect timeout) →
  `BEGIN IMMEDIATE` → in-transaction `CREATE TABLE _pp_ready_probe(x)`
  → `ROLLBACK`, zero business writes. [IMPL NOTE, this batch, probed on
  Windows under BOTH journals] reservation alone is NOT the falsifier:
  sqlite ACCEPTS `BEGIN IMMEDIATE` on a chmod-444 database (mode=rw
  opens it too) and only denies the write — so the probe performs one
  in-txn DDL (rolled back, zero residue, pinned by test) and a
  read-only file therefore maps to 503, not false-green 200. Success → 200
  `{status, service, probe:"ready", db:"ok"}`. REVOKED: `checked_at` —
  the server's ScenarioClock.now() PERSISTS to clock_session, making
  readiness itself a write path (and it could stall behind the very
  write-lock we are probing). If a timestamp is ever wanted it must be
  `time.monotonic()`-labelled non-authoritative wall; default is none.
  Busy/timeout/SQL error/corrupt → 503
  `{status:"not_ready", db:"error:<class>"}`. Tests, no mocks:
  (a) second connection holds `BEGIN IMMEDIATE` → ready 503, release →
  200; (b) DB file/chmod or directory made unwritable (POSIX) or the
  path opened read-only → ready MUST NOT report 200 — the probe itself
  gets SQLITE_READONLY/CANTOPEN and maps to 503;
  (c) probe performs no INSERT/UPDATE (instrumented: rollback-only);
  (d) Batch-A P1-1: target path MISSING → 503 AND the file still does
  not exist after the probe (no phantom creation, no sidecars).
- `GET /health/deep` — authenticated (`self._auth("plan")`, so
  unauthenticated → **403**, this codebase's convention), always 200
  with `overall_ok: bool`; sub-probes: verify_audit (read-only walk
  §5), chain head vs recomputed, clock.status() REAL shape
  (`kind/now/scenario/uptime_seconds` + conditional `session` sub-dict), agent status
  configured flag, backup age from the §5 V3 manifest. [BATCH-A P1-3]
  ONE aggregation semantic — `overall_ok` is the conjunction of every
  gate, no second core formula: audit-chain/head break, a clock or
  idempotency query that RAISES, and (production) an unconfigured agent
  or a missing/unreadable manifest each append to `gate_failures`;
  in DEVELOPMENT an unconfigured agent and a missing manifest are
  expected (local fallback) and do NOT flip overall_ok. Manifest age
  parses the RFC3339 `verified_at` field (an explicit `*_epoch` float
  is honoured only with that suffix; a missing/unparsable stamp is a
  gate failure, NEVER age 0 — the old epoch-only lookup false-greened
  every real Phase-4 manifest at ~0). HTTP stays 200 in every case.
  Deep is diagnostics; it is structurally never a probe target.
- Dockerfile HEALTHCHECK and deploy-gate probes MUST target
  `/health/ready`; compose api service additionally mounts backups
  read-only because deep exposes backup age — age comes from the
  `last_verified_backup.json` manifest written by §5 V3 (P1-4), NEVER
  from newest-file mtime; so api needs the `:ro` volume.

## §5 Phase 4 — backup verification + crash-safe restore (P0-2 rebuild)

**V1 read-only verifier (verifier must not repair verifiee):**
`verify_audit_connection(conn)` in `src/planpilot/audit.py` — pure
function over an already-open connection: SELECT-only walk of
audit_chain using the F5 verbatim rule (hash STORED record bytes,
never re-canonicalize), head equality; raises
`AuditChainError(kind=missing_table|missing_head|chain_break|
head_mismatch|…)`. NEVER CREATE, never INSERT OR IGNORE, never touches
the Database class. `tools/verify_backup.py` runs the FULL backup
battery (P1-3 — audit chain alone is not enough: a file with a valid
chain but a corrupt/missing authority/receipt/registry table must not
pass):
1. refuse if `<name>-wal` or `<name>-shm` exist NEXT TO the backup copy
   (a stranded sidecar means the copy is not self-contained);
2. `PRAGMA integrity_check` must return exactly `ok`;
3. required-tables checklist (grep-verified at `855a5c0`, from
   persistence.py + audit.py + factory_state.py executescripts):
   `audit_chain, audit_chain_head, authority_state, clock_session,
   publication_receipt, idempotency_registry, security_events,
   decision_traces, factory_states` — all present via SELECT from
   sqlite_master (read, not create). POLICY (round-4, deliberate):
   only a FULL runtime backup is restorable — a bare-`Database`
   fixture that never ran AuditTrail/FactoryState init is
   intentionally NOT restorable; Phase 4 builds its backup fixtures
   through the full runtime path, and the verdict text says which
   tables were missing;
4. `verify_audit_connection` chain+head walk (V1);
5. print report; exit 0/1.
Opens `file:…?mode=ro&immutable=1` + `PRAGMA query_only=ON`. Post-run
SHA-256 of the file MUST equal pre-run SHA (test-asserted for EVERY
battery outcome: pass, missing head, tampered record, whitespace-only
record mutation [must FAIL per F5 — proves no re-canonicalization],
broken link, deleted head row, missing required table, integrity_check
failure, stranded -wal).

**V3 scheduled backups enter the verified set only through V1 (P1-4):**
`tools/scheduled_backup.py` today prunes by mtime with zero
verification (F4). `backup_database()` KEEPS its `-> None` signature — it does not
return a path; the scheduled tool derives the deterministic
`<stem>-<utc-yyyymmddTHHMMSS><ext>` name from its own inputs (same
naming backup.py already uses), computes SHA-256 of the written
file, and passes the path to the V1 battery
(round-4: manifest is assembled by the scheduling TOOL in
scheduled_backup.py — no public API change needed). New behavior: right
after each backup file is created, run the V1 battery READ-ONLY
against it; verification failure
means the file is moved to `<dir>/unverified/` (rename) and NEVER counted
in retention's "success" set nor advertised. On success, atomically
(temp→fsync→replace) write `<dir>/last_verified_backup.json`
`{path, sha256, verified_at, chain_head:{entry_count,event_hash}}`.
Deep health's backup-age probe MUST read this manifest (never newest
mtime). Tests: corrupt-copy regression (write a valid-name backup then
tamper → not in verified set, manifest unchanged); negctl row flips
"verify after create" to pass-through → must-fail.

**R1 mutual exclusion:** server acquires a cross-process OS lock at
startup on `<db>.server.lock` (opened + `msvcrt.locking(LK_NBLCK)` on
Windows / `fcntl.flock(LOCK_EX|LOCK_NB)` POSIX — platform-branched
helper; stale-free by design: OS releases on process death). `run`
refuses restore while the lock is held by someone else (non-blocking
try). A pidfile is OPTIONAL HINT ONLY (printed in errors), never the
safety mechanism. Test: live Server subprocess holds the lock →
restore exit nonzero "server appears to be running".

**BOOT ORDER (round-4 P0-2 — the lock must precede EVERY writer).**
Today `api_server.main()` constructs `Database` at :409 (which runs
DDL + head repair) BEFORE `Server(...)` at :414 — if the lock lived in
`Server`, the db would already be mutated before it was guarded, and a
restore could race the pre-lock window. The G4 entry point therefore
pins the sequence:
```text
read env once → parse_startup_env + non-DB preflight
→ acquire OS DB lock → check marker/receipt/ACK
→ construct Clock → construct Database → construct services
→ bind HTTP socket
```
Shutdown: stop HTTP → close Database → release OS lock LAST.
Tests: (i) lock-denied or gate-refused startup leaves `Database.__init__`
NEVER called (audit-hook/spy assertion), socket never bound, restored
file SHA byte-identical; (ii) a restore attempting the same lock
mid-startup fails fast.

**R2 crash-safe replace protocol (P0-3 rebuild: intent ledger +
reconciliation).** Marker sidecar `<db>.restore-state` (JSON, written
temp→fsync→replace→parent-fsync) is NOT just a phase label. It is an
**intent ledger**: `intent_phase` (what the tool is ABOUT to do) plus a
per-file fact table `{path → {location: source|staging|quarantine|target|
absent, sha256, size, mtime_ns}}` for source backup, incoming staging,
and target main/-wal/-shm (each quarantine entry names its generation
dir). Every file MOVE is preceded by a marker write declaring it
(`pending_move` list) and followed by a marker rewrite updating the
facts — so at any hard-kill instant, the on-disk marker + actual file
positions are mutually consistent enough to RECONCILE.

**Reconciliation-first rerun (the only entry logic):** a rerun NEVER
trusts the phase label blindly. It (1) scans all locations, hashes every
present file, (2) diffs reality against the ledger, (3) derives the
state: every declared move either fully done (facts match) or not
started (pre-move facts match); (4) executes: finish remaining moves in
order, or roll back to idle. Partially-done individual moves are the
reason quarantine trio files are moved ONE AT A TIME (main, then -wal,
then -shm), each ledgered — a half-moved trio is reconstructable because
each file's true location is recorded. Same rule for the staging→target
`os.replace`: after it, staging is consumed — the ledger's fact
`target.main_sha == staging_sha` proves REPLACED even if the phase field
still reads QUARANTINED; rerun then skips re-missing staging (it is gone
by design) and proceeds to receipt.

Phases (facts, in order): `(missing)` idle → PREPARED (staging copy of
backup created + read-only verified + SHA == source; target untouched) →
QUARANTINED (trio moved one-by-one into fresh unique dir
`<db>.quarantine/<UTCts>-<seq>/` keeping original filenames) → REPLACED
(staging `os.replace`'d onto target path, same volume, fsync target +
parent) → RECEIPTED (receipt R3 fsynced; marker cleared last, after
receipt durable).

**Hard-kill matrix (all six, each rerun-to-convergence):** kill at (1)
after marker-create, before staging; (2) after staging, before first
quarantine move; (3) after target-main moved, before -wal; (4) after
-wal/-shm partially moved; (5) after `os.replace` staging→target,
before phase update to RECEIPTED-side; (6) after receipt durable,
before marker clear. For EACH: assert (a) reconciliation classifies the
world correctly from marker+files (no double-move, no missing-file
crash), (b) re-running restore converges (target SHA == backup SHA,
trio complete in one quarantine generation dir with original names,
marker cleared, receipt valid), and (c) a rollback variant restores the
pre-restore state byte-exact. Tests inject `os._exit` mid-sequence.

**R3 receipt:** `<db>.restore-receipt.json` (sidecar — NOT a new table;
rev.2's "keyset table" wording retracted), written temp→fsync→
replace→parent-fsync, binding: `backup_sha256`, `backup_path`,
`target_db`, `audit_head_entry_count + event_hash`, `quarantine_dir` +
`generation` (the ts-seq), `restored_at`, `tool_version`. Tamper test:
edit any receipt byte → ack's stored `receipt_sha256` no longer
matches the file → gate rejects.

**R4 recovery gating — no HTTP server at all until acked (P0-2 rebuild):**
`Server` startup (before bind) checks marker/receipt: a restore happened
(receipt present, or marker ≠ cleared) and no matching ack → **startup
aborts with an operator message in EVERY environment (dev included)**.
**ACK is per-restore, not per-environment-variable (round-4 P0-1).**
The ONLY ack path: `tools/restore_database.py --ack <operator>`
atomically (temp→fsync→replace→parent-fsync) writes
`<db>.restored-acked` binding `receipt_sha256` (the receipt file's own
hash — so ANY receipt byte-edit voids the ack), `generation`,
`backup_sha256`, `target_db`, operator and timestamp. `operator`/`timestamp` are PROVENANCE
only (no external truth source — a same-format edit to them is not
claimed detectable, round-5); the SECURITY-BINDING fields are
`receipt_sha256`, `generation`, `backup_sha256`, `target_db`. And
`restored_db_sha256_at_ack` — the LAST being ack-time EVIDENCE ONLY,
never re-compared at boot (a live DB is legitimately mutable; an
equality check at every start would false-refuse the first restart
after any write). Every NEW restore invalidates the previous ack
atomically BEFORE it first touches the target files (an `ack-invalidate`
op inside the ledger, so the invalidation itself survives a kill).
Two restores of the SAME backup therefore each require their own ack.
`PLANPILOT_RECOVERY_ACK` (rev.4's env bypass) is REVOKED entirely — a
leftover variable silently re-passing a second restore is exactly the
hole it opened. Malformed (unparseable) / missing /
stale-generation ack, or ANY mismatch on the four security-binding
fields → gate rejects → startup aborts (round-5: same-format edits to
the provenance-only operator/timestamp fields are NOT claimed
detectable — no self-hash or signature is asserted over the ack
file itself).

REVOKED (was rev.3's loophole): the `PLANPILOT_RECOVERY_DIAG=1`
diagnostics-only server. Its fatal flaw: `Database(path)` construction
ALREADY mutates the very file under recovery — executescript DDL,
`INSERT OR IGNORE` head repair (persistence.py:134), trigger installs —
and ScenarioClock attach/status can write `clock_session`. "Read-only
diagnostics on a db nobody has acked" is an oxymoron in this codebase.
(If a future rev ever wants an online diagnostics mode it needs a
separate ro-URI HTTP server that constructs NONE of Database /
RuntimeAuthority / ScenarioClock and opens everything
`mode=ro&immutable=1` — out of scope here.)

Diagnostics move OFFLINE, as commands the operator runs against the
un-acked file WITHOUT mutating it: `tools/verify_backup.py` (V1/V3),
`tools/restore_database.py --status` (marker+receipt+quarantine
listing, read-only), audit read-only walk (V1), receipt inspection
(`--show-receipt`). Boot gate: missing / malformed (unparseable) /
stale-generation ack, or ANY mismatch on the four binding fields →
startup aborts (round-5 narrowing: no all-bytes claim).
Tests: (a) unacked receipt → Server boot raises
before bind in dev AND production; (b) `--status`/verify against the
unacked restored file leave its SHA byte-identical; (c) after ack →
boot proceeds.

**Stale-state semantics:** restore to an older backup legitimately
reintroduces its own approval/idempotency state (time travel BY DESIGN,
contract has no cross-backup continuity requirement) — but it can never
be silently ready: ack must name the exact backup SHA, receipt shows
audit head, and audit of the restored file is chain-valid. Documented
in README; no publisher/authority core changes.

## §6 Phase 5 — deployment truth (real files) & EVAL locks

- **Dockerfile:** HEALTHCHECK changes from `/health` to
  `/health/ready`; pip layer switches to wheelhouse install
  (`pip install --no-index --find-links /wheelhouse`) built by
  `tools/build_wheelhouse.py` (pip download into an OUT-OF-REPO wheel
  dir, linux platform tags); **the `.whl` files themselves live
  OUTSIDE Git** (bundle/clone must not bloat — ortools is binary); the
  ONE repo-tracked layout (round-4 P1-5 — three competing names were
  drifting): `deploy/wheelhouse/MANIFEST.json` (tracked; filenames +
  sha256 + version pins) + `tools/build_wheelhouse.py` (tracked) +
  `<out-of-repo wheel dir>`; the docker build gate verifies manifest
  hashes before build; compose
  api service mounts `backups:/backups:ro`.
- **Deploy gate** `tools/deploy_gate.py --profile {development,
  production}`: LAYER 1 pure `evaluate_env(env) -> findings`
  (unit-testable); LAYER 2 I/O probes (git `ls-files` scan for secret
  patterns, key-file is outside repo + not a symlink + on POSIX mode
  ≤0600 — Windows substitutes "not repo-tracked + not symlink" and
  RECORDS the platform limitation in the finding), LAYER 3 optional
  `--gate=http` live `/health/ready` + `--gate=docker` (compose
  config + docker presence; **docker absent → status BLOCKED, exit
  2 — never a silent pass**). Bedrock: accepts EITHER
  `PLANPILOT_BEDROCK_API_KEY` OR `PLANPILOT_BEDROCK_KEY_FILE`
  (contract explicitly allows the emailed key from env);
  `--profile production` must agree with `PLANPILOT_ENV=production`
  (mismatch = finding).
- **EVAL:** run_evals.py bytes stay UNCHANGED (hash-locked by test);
  [LP] `30 BLOCKED / 0 PASS / exit 1` re-asserted by test; smoke
  harness stays a separate script/dir pair with its existing
  output-separation guard, now also test-pinned. Raw AWS evidence,
  when it eventually exists, lands OUTSIDE the repo; inside the repo
  goes a redacted manifest (case ids, timestamps, result, artifact
  SHA-256, storage URI — no keys/tokens/payload PII). Formal EVAL
  remains BLOCKED until a real Lightsail/Bedrock run — this spec's
  completion never implies EVAL PASS.

## §7 Naming hygiene (no invented contract status codes)

`RECOVERY_PENDING`/`CONFIG_REFUSING_START` were retracted. Internal
only: `RecoveryGateState.PENDING` (enum), `StartupConfigError`
(Exception), report values lowercase snake (`recovery_pending`,
`config_invalid`). One sentence in each doc that touches them: these
NEVER enter contract `tool_error.error_code` or plan lifecycle enums;
the closed-vocabulary guard + contract tests pin that.

## §8 negctl (must-fail mutations, added to the §10 register; hold suites stay socket-free)

1. `/health/ready` DB-busy path flipped to 200 → tests must fail.
2. restore skips read-only chain verification → must fail.
3. ack gate deleted (startup serves despite receipt) → must fail.
4. production secret/ENV gate short-circuited → must fail.
5. trigger-shape digest guard neutralised (no-op trigger accepted) →
   must fail.
6. [hold] clock/publisher suites pass unchanged.
Exact must-fail/hold counts pinned AFTER Phase 5 implementation in the
tasks.md Phase 6 checklist (no number claimed now).

## §9 Evidence & closure (G3 discipline inherited)

Freeze → runs (targeted → `tests/unit` → full → negctl → vocabulary →
live docker/eval probes) → logs with EXIT markers → EVIDENCE.json
final-state-bytes SHAs → **evidence-body commit** → verify against
final Git blobs (+ no-.git harness copy + bundle→temp-clone) →
**attestation commit** ticking verification-class boxes naming the
exact verified SHA → deliverable bundle kept OUTSIDE the repo under
`D:\PlanPilot_backups\` with a `#`-commented `.sha256` sidecar.

## §10 Test register (what proves each claim)

| Design claim | Test |
|---|---|
| A1 row-missing raises | sandbox subprocess (guards intentionally absent — design §2 A1), never an intentionally-red normal test |
| A2 three triggers + shape guard | unit: tamper matrix on live Database (aborts); hollow same-name trigger planted pre-start → digest check raises; drop-and-fail sentinel in negctl sandbox |
| A3 tamper matrix + guard-absent sentinel | unit: raw-SQL matrix on real columns; the drop-triggers-then-bypass-MUST-fail check lives in the negctl sandbox/subprocess, never as an intentionally-red main-suite test |
| health split | unit http: /health body unchanged; live/ready/deep semantics; external write-lock → ready 503 → release 200 (real subprocess conn); **ro-opened probe falsifier (BEGIN ok but INSERT denied) proves ready is not ro-based**; read-only dir/files → 503 |
| V1 verifier | unit: negatives + SHA-before==SHA-after; verifier source has no CREATE/INSERT tokens (AST check) |
| R1-R4 | unit: SIX kill points × {resume, rollback} convergence on a
  second restore; boot-order proof (no Database.\_\_init\_\_, no bind,
  SHA stable when lock/gate denies); ack binding tamper (receipt byte →
  ack void; ack malformed → reject; each security-binding field
  edited in isolation → reject; operator same-format edit → accepted
  as provenance per round-5; second restore of same backup → fresh
  ack required) |
| R4 | unit: startup aborts without ack (dev too), socket never bound; ack SHA mismatch aborts; recovery-gate CLI exits non-zero on a normal DB; restored file SHA unchanged across any gate/CLI call (no Server/Database ever constructed on an unacked path) |
| §3 config | unit: parse purity (dict only), closed env, secret repr, preflight factory_root-is-dir |
| §6 docker | gate docker profile: build from wheelhouse manifest, container boots, ready probe green; docker missing → BLOCKED exit 2 |
| §6 EVAL | run_evals bytes hash-locked; 30/0/exit1 pinned; smoke separation pinned |
| §7 | contract error_code enum + lifecycle enum untouched (existing closed-vocab + schema tests) |

## §11 Consistency & retracted-wording register

- Rev.2 chat wording retracted as not-in-design: `/api/v1/health`,
  EVAL `exit 77`, `--env local-fake`, ACK-bypass of run_evals,
  `startup_config.py`/`deploy_gate.py` as sole module names (kept
  `deploy_gate.py`; config lives in `tools/api_server.py` +
  `src/planpilot/startup_config.py` — FINAL: new module
  `src/planpilot/startup_config.py`), "no module imports socket server
  code" (false — the server IS a ThreadingHTTPServer; negctl HTTP-free
  wording stays scoped to hold suites per P3).
- Rev.2 invented facts fully struck: host/pid/txn_id/boot_id/version
  columns; db.clock/clock_history/clock_service_session; digest_history
  + approval_records_source_key_check; silent-anon auth; read-only
  /health; verify_audit repairs head; backup returns dict;
  status.json; exit 2 evals; docker-compose.yml name; Sonnet 4.6 /
  Agent Runtime; results/agent_eval.
- This table is the single source the README/tasks/START_PROMPT were
  rewritten against; any residual phrase from the struck list found in
  the four docs is a defect.
- REV.4 retraction register (round-3 findings, all personally re-run).
- REV.5 retraction register (round-4 findings — 2 P0 + 6 drifts, all
  re-verified against source at 855a5c0 before adopting).
  * `[LP-1]` "clean close leaves -wal" — FALSE; reviewer and my re-probe
    both show a fresh temp DB closes to just `<db>`. WAL siblings appear
    only with open transactions/crash/other connections. Replaced by
    conditional wording; restore handles the trio unconditionally.
  * audit hash `sha256(previous + canonical(record))` — WRONG; actual
    (persistence.py:224) is `sha256((previous + row["record"]).encode())`
    over the STORED string. Re-canonicalizing in a verifier silently
    accepts whitespace tamper; F5 now states the verbatim rule.
  * `checked_at=self.clock.now()` in /health/ready — REVOKED (clock.now()
    WRITES clock_session; a readiness probe must be write-free, and the
    call can block past the probe budget under external lock).
  * `mode=ro` readiness probe — REVOKED (reviewer measured: ro connection
    can BEGIN but cannot write). ready is now an rw BEGIN IMMEDIATE/
    ROLLBACK probe.
  * `PLANPILOT_RECOVERY_DIAG` diagnostics-only server — REVOKED (opening
    Database repairs the head + runs DDL on an unacked restored file).
    Recovery gate is now zero-HTTP; diagnostics are offline CLIs only.
  * `SecretStr` — not stdlib; replaced with `field(repr=False)`.
  * "dev boots without secret" — overstated; Server requires a valid
    ≥32-char secret in ALL envs today; only the ENV BINDING gate is new.
  * hand-written env inventory naming `PLANPILOT_HEALTH` /
    `AUTHORITATIVE_RUNTIME` — those have ZERO read-points (measured);
    inventory is AST-generated (task 0.4) and RE-EMITTED after G4
    lands — a count frozen in prose (rev.4's "19 vars / 21 points")
    is a snapshot, NOT a permanent invariant, and the rev.4 regex
    generator provably misses indirect reads (clock.py:231 mapping
    arg, publisher FAULT_ENV_VAR constant indirection at :337/:363).
  * rev.4's `PLANPILOT_RECOVERY_ACK` env var — REVOKED in rev.5
    (round-4 P0-1): a stale env would auto-pass the SECOND restore of
    the same backup. Ack is per-restore, file-sidecar only.
  * rev.4 boot order implied lock-inside-Server — WRONG against
    today's `main()` (Database@:409 before Server@:414); rev.5 pins
    env→parse→LOCK→gate→Clock→Database→bind (round-4 P0-2).
  * rev.4 tasks `port 8731` / `ready_timeout_s 0.5` — ported typo;
    truth: live port 8080 (api_server.py:414, Dockerfile, compose),
    single budget `ready_timeout_ms=1500`.
  * rev.4 tasks claimed `BLOCKED:` stdout line — run_evals.py prints
    only the readiness-line + `cases=… blocked=30` summary (:72-73);
    acceptance now quotes the REAL lines.
  * rev.4 tasks pointed smoke output AT the formal-eval dir — real
    default is `tests/evidence/compact-smoke` (run_smoke_harness.py:22)
    which already REFUSES runtime-eval; corrected.
  * rev.4 "Dockerfile:31/compose:35 now set PLANPILOT_ENV" — they do
    NOT (grep-verified); it is a Phase 2/5 ADDITION, stated as such.
  * ACK "any byte/field edit detectable" — OVERBROAD (round-5): an
    unforgeable receipt hash is NOT a self-hash of the ack file;
    operator/timestamp have no external truth source. Narrowed to
    "malformed or security-binding-field mismatch rejects"; four-field
    falsifiers added to the ack regression set.
  * phase-label-only marker — INSUFFICIENT (4 open kill windows);
    replaced by the intent ledger + filesystem reconciliation.
  * "spec commit = task 0.6" — process retracted: spec commit is a
    standalone prerequisite created after reviewer approval; Phase 0
    verifies ancestry, not HEAD equality. Evidence staging outside the
    repo until Phase 6.

## §12 Reviewer-preserved decisions (carried forward unchanged)

Read-only verifier that never repairs; live/ready/deep split; ready
503 on real lock contention; EVAL runner zero-change, no bypass flags;
gate pure-vs-I/O layering; tooling absence = BLOCKED not PASS; G3
dual-commit evidence discipline.
