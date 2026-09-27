# Tasks — `production-gates`

**Design:** `./design.md` (rev.5 — round-4 fixes: per-restore ACK
sidecar w/ no env bypass, boot-order lock-before-Database, doc
drift alignment; architecture unchanged)

**Contract:** frozen, `b92e53f4...fe639`.
**HEAD before this spec:** `855a5c0` (sealed G2/G3).

## Execution protocol

1. **Reviewer Gate (BEFORE any phase).** rev.5 passes review; THEN the
   reviewer approves the **spec commit as a standalone prerequisite
   commit** (all four files README/design/tasks/START_PROMPT + the
   devlog entry). It is not task 0.x. After it, `HEAD != 855a5c0` and
   that is expected; Phase 0.1 verifies `855a5c0` is an ANCESTOR of
   HEAD, never that HEAD still equals it.
2. Baseline contract `b92e53f4...fe639`; baseline tag `g2-baseline-5bf299a`
   derefs to `5bf299a`; `git diff --check` clean per commit; add no
   `# type: ignore` / `noqa`; closed vocabulary PASS; the frozen contract
   error-code and lifecycle enums stay untouched.
3. Unit/e2e tests touch no sockets; negctl and the §5 probe are
   subprocess/sandbox; HTTP tests are hold-socket-free (`bind_and_serve`
   trips them) and live in p2-5-style suites.
4. Full suite serial; negctl alone. Reports quote raw stdout byte-for-byte.
   Any rev.3 claim that fails an anchor check is retracted, not reworded.
5. Evidence: probes/logs are written OUTSIDE the repo (staging under
   `%LOCALAPPDATA%\Temp\g4-staging\`), and copied INTO
   `tests/evidence/g4-production-gates/` ONLY in Phase 6, where the dir
   becomes its final tracked state. No premature evidence dir.

## Phase 0 — Fact verification (against HEAD; spec already committed)

- [x] 0.1 `git merge-base --is-ancestor 855a5c0 HEAD` passes (ancestor,
      not equality); contract SHA unchanged; baseline tag derefs `5bf299a`.
- [x] 0.2 §0 fact table re-verified line by line at HEAD (clock.py
      277L / persistence.py 231L / api_server.py 425L / audit.py /
      backup.py / scheduled_backup.py / run_evals.py / Dockerfile /
      compose.yaml / real columns / 3 triggers / routes / auth /
      verify_audit read-only / repair-at-init:134 / backup_database
      returns None / Sonnet 4.5).
- [x] 0.3 Re-run the two [LP] probes and pin them in a log:
      (a) `Database` open→close on a fresh temp db with NO open
      transaction => dir has ONLY `<db>` (no -wal/-shm); and open a
      write txn WITHOUT close => `probe.db-wal`/`-shm` DO appear
      (confirms P1-1: WAL presence is conditional, restore must handle
      all three regardless).
      (b) `run_evals.py` => `cases=30 passed=0 failed=0 blocked=30`,
      EXIT=1. stdout has NO `BLOCKED:` line — acceptance matches the
      REAL runner output (run_evals.py:72-73):
      line 1 `formal EVAL readiness evidence:` — assert the ABSOLUTE
      path (prefix = ROOT, suffix = tests/evidence/runtime-eval, per
      run_evals.py:17, never a hardcoded relative string); line 2
      EXACT `cases=30 passed=0 failed=0 blocked=30` + exit 1.
- [x] 0.4 Inventory generator: `tools/probes/inventory_env.py` is an
      **AST walk** (not a regex) over `src tests tools`: counts actual
      read CALLS of `os.environ`/`environ` Subscript and `.get`,
      resolves module-CONSTANT indirection (publisher.py:337
      `FAULT_ENV_VAR` -> PLANPILOT_PUBLISHER_FAULT read at :363) and
      mapping-parameter reads (clock.py:231 `environ.get` inside
      `clock_from_env(environ, ...)`); ignores comments, docstring
      examples, and test fixture dicts never read. Emits per-file
      table + totals, dual scope (production-startup subset / all
      read-points). The 855a5c0 snapshot (19 vars / 21 points) is a
      BASELINE for comparison only — RE-EMIT after G4 lands (new
      ENV/backup/ready/recovery vars will appear); never hard-code it
      as a permanent invariant in tests or docs.
      The design F-table cites THIS output, not a hand list. Confirms
      PLANPILOT_HEALTH and AUTHORITATIVE_RUNTIME have ZERO read-points
      (round-3 retraction upheld) and the Bedrock family
      (API_KEY/KEY_FILE/REGION/MODEL/DAILY_TOKENS/FORBID_LLM_NETWORK),
      clock family (MODE/SCENARIO_NOW/DATASET/ALLOW_PUBLIC_SCENARIO),
      CONTRACT_PATH/SHA256, and FAULT_ENV_VAR=PLANPILOT_PUBLISHER_FAULT
      (publisher.py:337) are all captured.

## Phase 1 — clock_session defense-in-depth (design §2)

- [x] 1.1 A1: edit `clock.py::_persist` return-None fail-open => raise
      `RuntimeError` when the anchor row is gone; `_read_row`-None
      attach/resume path untouched; clock.py stays stdlib-only.
- [x] 1.2 A2: three triggers on the REAL 4 columns, reusing the
      `RAISE(...)` precedent verbatim, inserted at persistence.py:133.
- [x] 1.3 A2 shape guard: `Database.__init__` computes sqlite_master SQL
      digests for the three; mismatch (incl. pre-seeded no-op) => raise.
      Same-named existing row with different SQL => `CREATE TRIGGER`
      silently no-ops (empirically pinned), so the guard is mandatory.
- [x] 1.4 unit: delete/anchor-rewrite/time-rewind aborts (`IntegrityError`,
      empirically pinned) + forward UPDATE regression pin + blanket
      `UPDATE ... WHERE 1` cannot reanchor; hollow-trigger pre-plant =>
      digest raises; missing-at-launch self-heals (design §2 1.0.1-#1
      semantics — NO "every missing trigger RuntimeError" claim);
      RENAME pin: raw SQLite `ALTER TABLE ... RENAME COLUMN` SUCCEEDS
      (BEFORE triggers never fire on DDL — NOT aborted at the moment),
      SQLite rewrites the stored trigger SQL, and the NEXT
      `Database(...)` open fails closed on the shape digest; A1 row-drop
      raises (guards absent) via sandbox subprocess, never a red main
      test. Failed opens release the handle (file immediately
      rename-/deletable on Windows, no gc.collect()).

## Phase 2 — startup config (design §3)

- [x] 2.1 `StartupConfig` frozen dataclass, real secret
      `field(repr=False, compare=False)` (NOT pydantic SecretStr — no new
      dep); `PLANPILOT_ENV` closed to the tuple ENVS
      ("development","production") — a stdlib runtime check, not a
      `typing.Literal` annotation (same guarantee, no import ceremony);
      defaults host 127.0.0.1 / port 8080 (the REAL live default —
      api_server.py:414, Dockerfile, compose all 8080; rev.4's "8731"
      was a typo) / ready_timeout_ms 1500 (single ms budget for the
      rw probe's busy_timeout AND its connect timeout; rev.4's "0.5s"
      contradicted design's 1500ms) / clock mode wall /
      backup_dir default: development = `ROOT/backups` (ROOT = repo
      root derived from module location; matches backup.py:290 — the
      current production default and the compose bind target).
- [x] 2.2 `parse_startup_env(dict)->tuple[ParsedConfig|None,list[Issue]]`
      PURE; `preflight(cfg)->list[Issue]` does all I/O; factory_root is
      a DIRECTORY (is_dir), backup_root a DIR; auth required all envs
      (>=32 chars, hard-reject not bypass, warn only on localhost);
      production non-loopback + backup existence. Return types unified
      tuple across design AND tasks (round-3 P1-5).
- [x] 2.3 api_server `main()` one snapshot; `Server.__init__` consumes
      cfg (Batch-A P0 fix: the legal path passed `cfg.backup_root` —
      a field that does not exist on StartupConfig; the real name is
      `backup_dir`, now pinned by a main()-assembly test that runs the
      VALID config end to end); `PLANPILOT_ENV` set (Dockerfile/compose
      do NOT set it today
      — grep-verified; Phase 5 ADDS `PLANPILOT_ENV=production` to both
      actually read, not just set); `config_provenance()` redacted
      (P2 wording: non-sensitive values + paths are logged; the secret
      only as present/length — never booleans/counts-only, which
      understated what the record carries).
- [x] 2.4 unit: parse purity (dict only, no fs); dev with a VALID secret
      boots, dev WITHOUT one still rejects (never claimed to boot);
      prod localhost/missing-secret => StartupConfigError (Batch-A
      P1-2: the loopback REFUSAL is now implemented in parse as a
      STRUCTURAL check — ipaddress is_loopback incl. IPv4-mapped, plus
      casefolded/trailing-dot localhost (review-3 #2); Phase 5 compose
      binds 0.0.0.0); repr never
      leaks the secret. Bedrock per-call key re-read is an explicit
      documented exception (rotation), not folded into the boot snapshot.

## Phase 3 — health split (design §4)

- [x] 3.1 `/health` byte-for-byte unchanged (incl. SELECT 1 under
      db.lock; the one consumer test:157 unaffected); add `/health/live`.
- [x] 3.2 `/health/ready`: independent per-call connection opened as a
      `file:<resolved-path>?mode=rw` URI (`uri=True`) — Batch-A P1-1
      review falsified the previous PLAIN `sqlite3.connect(path)`: it
      silently CREATES a zero-byte phantom DB when the file vanished
      and readiness then false-greens it. `mode=rw` refuses a missing
      path (CANTOPEN => 503, file still absent after the probe — test
      (d) + negctl mutation #6) and on an existing file is exactly the
      default RW open the probe needs. ONE budget
      `ready_timeout_ms` for connect timeout AND
      `PRAGMA busy_timeout`, `BEGIN IMMEDIATE` -> in-txn CREATE
      falsifier -> `ROLLBACK`
      -> 200 `{status,service,probe:"ready",db:"ok"}` (body per design
      §4; the tasks' old `ready:true` shorthand never matched the
      design); **NO clock.now() write, NO checked_at**;
      missing/locked/read-only/ro-open/corrupt => 503. Honest
      claim (round-4 + P1-1): exists + opens-RW + writer-reservation +
      write-capable — NOT a durability/free-space proof.
      Falsifier tests: an ro connection that wins a reserved lock but is
      denied the write must NOT be accepted as ready; a missing path
      must NOT be created by the probe.
- [x] 3.3 `/health/deep` auth via `self._auth("plan")` — the repo Bearer
      convention (rev.5b fact-check: an `X-PlanPilot-Token` header does not
      exist anywhere in this codebase; design §4 names `_auth`), so
      unauthenticated/weak token => 403 (no invented 401); always 200
      with `overall_ok: bool`; sub-probes: verify_audit (read-only walk
      §5), chain head vs recomputed, clock.status() REAL shape
      (`kind/now/scenario/uptime_seconds` + conditional `session`),
      agent configured flag, idempotency COUNT, backup age ONLY from
      `last_verified_backup.json` manifest (absent =>
      `no_verified_manifest`, NOT latest-file mtime; RFC3339
      `verified_at` parsed via `datetime.fromisoformat`, Batch-A P1-3 —
      the old epoch-keys-only reader false-greened real Phase-4
      manifests at age~0). [P1-3 ONE semantic] `overall_ok` aggregates
      EVERY gate: audit/clock-raise/idempotency-raise are failures in
      any env; unconfigured agent + missing manifest fail in
      PRODUCTION, warn in development (dev runs the local fallback by
      design). HTTP stays 200 with `gate_failures` listing names
      (never a readiness target).
- [x] 3.4 unit: /health body unchanged; three probes distinct; external
      write-lock (real second conn) -> ready 503 -> release 200;
      read-only FILE -> 503 falsifier (in-txn CREATE; reservation alone
      false-greens, empirically pinned on Windows — the read-only-DIR
      variant is POSIX-chmod-only and NOT claimed here); budget bounded
      by the snapshot timeout; zero-residue proof after success.

## Phase 4 — backup verification + recovery (design §5)

- [x] 4.1 `verify_audit_connection(conn)` pure SELECT-only over the
      connection (no ctor, no repair); hash = `sha256((prev +
      row["record"]).encode())` on the STORED record STRING; re-canonicalize
      mutation must be rejected. AST guard: verifier source has no
      CREATE/INSERT/UPDATE/DELETE.
      DELIVERED cbb484f + correction entry 2026-09-26: AuditChainError
      (kind: missing_table|missing_head|chain_break|head_mismatch);
      F5 verbatim stored-text hashing; guard = ast walk of the function
      source, keyword test asserts the guard itself bites.
- [x] 4.2 `verify_backup_file(path)->BackupVerification` — a SEPARATE
      read-only fn from `verify_audit_connection`: `mode=ro&immutable=1`
      + `query_only=ON`; requires `PRAGMA integrity_check == "ok"`;
      REFUSES if a `-wal`/`-shm` sibling exists beside the backup; core
      table presence (audit_chain, audit_chain_head, clock_session,
      security_events, decision_traces, publication_receipt,
      authority_state, idempotency_registry, factory_states); audit
      chain+head walk; backup SHA unchanged before/after.
      DELIVERED cbb484f + correction entry 2026-09-26: REQUIRED_TABLES
      = exactly the 9 tables above (the earlier "13 incl. runtime_events"
      devlog note was wrong and is corrected, not the code); every
      outcome — pass AND each refusal — leaves the file byte-identical
      (V1 never-repair pinned per-outcome). CLI tools/verify_backup.py
      exit 0/1/2 (PASS / battery FAIL / path error), smoke-proven on a
      real tampered copy.
- [x] 4.3 `backup_database()` STAYS `-> None` (design §5 V3 — no
      public API change); `tools/scheduled_backup.py` derives the
      deterministic backup filename from its own inputs, computes its
      SHA-256, then calls verify_backup_file on it AFTER each backup, excludes failures from
      the retention "successful" set, and atomically writes
      `last_verified_backup.json` (manifest binding path + sha256);
      deep-health age reads THIS manifest.
      DELIVERED 2026-09-26: battery runs BEFORE anything is advertised;
      refused file -> shutil.move into <dir>/unverified/ (quarantine
      gets its OWN bounded prune, same keep); manifest = atomic
      mkstemp+fsync+os.replace of {path, sha256, verified_at RFC3339-Z,
      chain_head:{entry_count,event_hash}} — field-by-field compatible
      with the existing _manifest_verified_epoch reader
      (api_server.py:284/312). The manifest sha256 is recomputed via
      sha256_file(target) at advertise time — the same chunked
      algorithm the battery used for its before/after invariant
      (BackupVerification.sha256 == sha_after), so the two agree by
      construction. tests/unit/test_scheduled_verified_set.py 5 passed.
- [x] 4.4 OS lock helper (msvcrt/flock) acquired by the ENTRY POINT
      BEFORE any Database/Clock construction (round-4 P0-2 boot order:
      env -> parse+non-DB preflight -> LOCK -> marker/receipt/ACK gate
      -> Clock -> Database -> services -> bind; shutdown: HTTP -> DB
      close -> release lock LAST), held for life; restore takes it
      NON-blocking (fail immediately = "server still holds it");
      pidfile advisory only.
      DELIVERED: `src/planpilot/db_lock.py` (msvcrt LK_NBLCK / flock
      LOCK_EX|LOCK_NB, non-blocking, OS-released-on-death, `.pid` hint
      written only while held and printed on refusal — never the
      mechanism). `api_server.main()` takes the lock after env parse +
      startup_or_die and BEFORE Clock/Database/Server; DbLockHeld ->
      SystemExit, zero construction, zero DDL side (dir-listing
      invariant). Shutdown mirror asserted on the OBSERVED event list
      [lock, clock, database, serve, http-stop, db-close,
      lock-release]. The marker/receipt/ACK gate slot (task 4.6)
      mounts between LOCK and Clock — position proven by the
      lock<clock ordering test; restore's non-blocking take uses the
      same helper when wired in 4.5.
      tests/unit/test_db_lock_boot.py 6 passed; unit 866 passed.
- [x] 4.5 Intent-ledger state machine (`<db>.restore-state`): every op
      logged pending/done with per-file SHA; a phase advances ONLY when
      all its ops are done; on reopen `reconcile_ledger()` re-derives fact
      from filesystem (main.db vs staging vs quarantine + SHA), never
      trusts the label. WAL/SHM moved file-by-file. `--rollback` path.
      Receipt temp->fsync->replace->parent-fsync binding backup
      SHA+target+audit_head+quarantine generation; any later byte =>
      gate rejects.
      DELIVERED 2026-09-26: `src/planpilot/restore.py` — nine-op ledger
      (ack-invalidate, stage-copy, stage-verify, quarantine main/wal/shm,
      replace-staging, receipt-write, marker-clear); declare-before /
      facts-after marker writes; reconcile re-derives every op from
      byte-level facts (replace-proven retroactively completes earlier
      ops — design kill#5 verbatim); stage-verify runs the 4.2 battery;
      rollback validates slot ownership (pre-move fact / anchored sha /
      quarantine copy) and refuses over unknown bytes with zero
      mutation. Tests: test_restore_ledger.py 21 passed (matrix cases
      land in 4.7).
- [x] 4.6 Zero-HTTP recovery gate (P0-2): NO unacked server starts
      (dev too; socket never bound, StartupConfigError-class refusal
      before bind). Diagnostics are OFFLINE-ONLY via the existing CLIs
      (`tools/verify_backup.py`, `tools/restore_database.py --status /
      --show-receipt`) — read-only paths that construct NO Server /
      Database / RuntimeAuthority / ScenarioClock; the ack file
      `<db>.restored-acked` carries receipt SHA + operator + ts +
      `restored_db_sha256_at_ack`; gate rejects missing/malformed ack or
      ANY mismatch on the four security-binding fields (receipt SHA,
      generation, backup SHA, target db); operator/timestamp are
      provenance, same-format edits NOT claimed detectable (round-5);
      publisher has no recovery-awareness code; PLANPILOT_-
      RECOVERY_DIAG and PLANPILOT_RECOVERY_ACK both DO NOT EXIST (both
      revoked; the ONLY ack path is restore_database.py --ack, round-4
      P0-1); the restored_db hash is ack-time evidence, NOT a boot-time
      equality constraint (a live DB must restart fine after writes).
      DELIVERED 2026-09-26: `evaluate_recovery_gate()` in restore.py +
      api_server.py main() slot (line 655 — after lock, before
      clock@672/Database/Server/bind; refusal is a SystemExit with the
      operator message, same class as the startup-config refusal it
      sits beside). ack-invalidate op voids a stale ack BEFORE target
      touches (kill-safe). CLI --status byte-frozen tests + source-level
      construct ban (docstring excluded, it names the absences).
      Revoked env vars: ZERO reads (grep environ/getenv + RECOVERY = 0
      hits; the only textual occurrence is the api_server comment
      stating the revocation itself). Gate/boot-order
      proofs land in 4.7.
- [x] 4.7 unit: kill-point matrix at SIX points (after marker-pre-stage,
      after stage, after main moved, after partial WAL, after replace-
      pre-phase, after receipt-pre-clear) => every case a SECOND restore
      converges to a consistent state; verifier/backup SHA invariants;
      core-table-missing => not restorable; ro-open BEGIN-ok-but-INSERT-
      denied falsifier for readiness. Plus the four round-4 P0-1 ack
      regressions (same-backup 2nd restore voids old ack; acked then
      written then restarted => boots; receipt byte-edit => ack void;
      ack malformed / each security-binding field edited in isolation =>
      reject) and the round-4 P0-2
      boot-order proofs (lock-denied/gate-denied startup:
      Database.__init__ spy never called, socket never bound, file SHA
      unchanged; mid-startup restore cannot take the lock).
      DELIVERED 2026-09-26: `tests/unit/test_kill_matrix.py` 22 passed —
      REAL subprocess `os._exit(9)` at each of six ledger seams mapped
      1:1 to the design points (stage-copy / quarantine-main /
      quarantine-wal / quarantine-shm / receipt-write / marker-clear);
      each seam gets (a) zero-conflict reconcile classification, (b)
      rerun-to-convergence (target==backup SHA, trio under ONE
      generation dir, marker cleared, receipt bound, gate opens after
      ack), (c) byte-exact rollback of main/-wal/-shm (siblings FORGED
      — a closed DB checkpoints WAL away; the ledger treats them as
      opaque owned bytes). Falsifier: ro+immutable URI open — BEGIN and
      SELECT succeed, INSERT/UPDATE/DELETE/DROP all driver-denied. The
      four P0-1 ack regressions are in test_restore_ledger.py (21
      passed, landed with 4.5/4.6); P0-2 gate-denied proof adds a
      can-restore-immediately check pinning the refusal's lock release.
      Matrix surfaced two real implementation bugs, fixed at the source
      and self-reported in devlog: gate refusal stranded the OS lock on
      the exception frame for in-process callers; ack-invalidate was
      deleting the CURRENT restore's own ack on resume (now generation-
      scoped). Unit merged at **909 passed** (887+22,
      g4-probe-staging/phase4-47-unit.log).

## Phase 5 — deployment boundary (design §6)

- [x] 5.1 `deploy/wheelhouse/MANIFEST.json` (tracked) + SHA +
      `tools/build_wheelhouse.py` (tracked) + out-of-repo wheel dir
      (single layout, round-4 P1-5 — old three-name drift retired)
      committed; binary wheels NOT in Git (build-artifact, out-of-repo).
      DELIVERED 2026-09-25: MANIFEST.json (27 linux wheels, sha256+sizes)
      + tools/build_wheelhouse.py; verify green against
      E:/PlanPilot-Hackathon/planpilot-wheelhouse (repo-external).
- [x] 5.2 `tools/deploy_gate.py`: pure logic (env, secret, path, lock
      rules) + I/O probes (docker gate incl. `compose config`, HTTP
      /health/ready, Bedrock credential, wheelhouse) per design §6 —
      Bedrock credential = API_KEY env OR a managed key file (both allowed);
      a check with no runnable target => BLOCKED (never a false PASS).
      DELIVERED 2026-09-25; 5.2.1 hardened same day: docker gate =
      daemon + `compose config` (live daemon + broken compose => FAIL,
      not PASS); HTTP /health/ready classifies a live non-green target
      (incl. 503) as FAIL, only unreachable => BLOCKED; key file must
      be a regular file passing BedrockClient content rules (a
      directory can no longer masquerade); build_wheelhouse `download`
      refuses a non-empty wheel dir. No AWS probe (real AWS = formal
      EVAL boundary). tests/unit/test_deploy_gate.py. 5.2.2
      2026-09-26: the ENV credential channel now runs the SAME token
      rules as BedrockClient._credential() (single-line, ASCII,
      <=16384, no whitespace; env values are NOT stripped, matching
      the runtime), so an invalid env key is FAIL, never PASS.
- [x] 5.3 Docker: Dockerfile HEALTHCHECK switches to `/health/ready`;
      compose api mounts backups read-only (deep age). **Docker is a G4
      acceptance item: docker absent => G4 stays BLOCKED; do NOT tick
      5.3 as G4-complete.** Only AWS/formal-EVAL are permitted non-goal
      BLOCKED edges.
      DELIVERED 2026-09-26 (LIVE, not static-inferred): host Docker
      29.8.0 + compose v5.5.1. Dockerfile: HEALTHCHECK→/health/ready,
      offline wheelhouse install (`--no-index --find-links`, in-image
      `build_wheelhouse.py verify` on all 27 wheels before pip), explicit
      PLANPILOT_ENV=production, `COPY data` (preflight refuses a missing
      factory root — image could never boot without it). compose.yaml:
      api production-profiled, PLANPILOT_ENV=production explicit,
      non-loopback publish (`${PLANPILOT_PUBLISH_ADDR:-0.0.0.0}:8080`),
      `backups:/backups:ro`, secret via required `:?` interpolation (zero
      plaintext), wheelhouse via named build-context `wh`
      (additional_contexts, default ../planpilot-wheelhouse out-of-repo,
      PLANPILOT_WHEELHOUSE_DIR override). Two integration defects found
      by the live run and fixed at the source: (1) preflight demanded
      WRITABILITY of PLANPILOT_BACKUP_DIR — contradicting the mandated
      :ro mount (production unbootable); backup dir is now a READ target
      (production: exists+readable; development: absent tolerated);
      (2) deploy_gate.probe_docker ran `compose config --quiet` without
      profiles (vacuous — profile-gated services skipped) and without
      satisfying the `:?` secret (missing secret misreported as broken
      compose file); now both profiles + subprocess-only placeholder.
      Live acceptance chain (evidence: tests/evidence/g4-production-gates/
      docker-live.log): build→up→/health/live 200→/health/ready 200
      db ok→deep 403 unauth→/backups touch refused + mount ro,relatime→
      HEALTHCHECK healthy ExitCode 0→restart→ready again→/audit/status +
      db sha256 byte-identical→deploy_gate live docker L3 PASS +
      http_ready PASS→down -v. Static regressions:
      tests/unit/test_deployment_static.py (7).
- [x] 5.4 EVAL: `run_evals.py` byte-unchanged (hash-locked);
      30 BLOCKED / 0 PASS / exit 1 stays (acceptance = the REAL
      summary lines, no `BLOCKED:` token exists); smoke harness output
      stays in ITS OWN default `tests/evidence/compact-smoke`
      (run_smoke_harness.py:22 — rev.4 wrongly wrote runtime-eval; the
      script already FORBIDS writing under runtime-eval, keep + test
      that guard); no new bypass switch.
      DELIVERED 2026-09-25: tests/unit/test_eval_freeze_54.py pins
      both tool SHAs (run_evals 6285b782…, smoke 47da9ba9… — file
      confirmed byte-clean vs HEAD adbeb48), real 30-blocked summary
      shape, compact-smoke≠runtime-eval default + write-forbid guard,
      and an exact `--output`-only argparse surface (no bypass flag).

## Phase 6 — evidence, negctl, closure

- [x] 6.1 negctl sandbox adds must-fail mutations, EXACT counts pinned
      HERE only: ready ro-probe false-green; restore skips verification;
      ack gate bypass; secret gate bypass; hollow same-name trigger
      accepted; [hold] clock/publisher pass.
      DELIVERED 2026-09-26: new tests/negative_control/
      test_g4_closeout_negctl.py adds ONLY the two §8 lines without a
      prior control — §8.2 restore skips the read-only battery
      (bare-db restore must fail) and §8.4 secret/startup gate
      short-circuited (31-char secret must be refused). §8.1 (ready
      ro/reservation/phantom false-greens), §8.3 (ack/marker/receipt
      gate bypass) and §8.5 (hollow trigger) are ALREADY controlled by
      test_clock_defence_negctl.py mutations 3-6 and
      test_recovery_negctl.py mutations 3-5 — referenced in the file
      header, never restaged (no duplicate theater).
      HOLDS: clock defence (test_clock_session_defence.py) + publisher
      audited transaction (test_publisher_schema.py) must STAY GREEN
      under every closeout mutation — both explicit, fast, socket-free
      (structural guard test_hold_suites_are_http_free); no
      expected_suite=None path exists.
      EXACT COUNTS (from raw stdout, negctl.log):
      g4-closeout caught=2 escaped=0 broken=0 held=2 of 2;
      whole tests/negative_control = **12 passed in 338.78s**
      (approval 14/0/0, clock-defence 6/0/0, g4-closeout 2/0/0+2 held,
      model-binding 6/0/0, plan-store 56/0/0 + 2 defence-in-depth held,
      recovery 5/0/0, security-audit 9/0/0, tool-middleware 11/0/0).
      Real-tree byte-restore hash-checked in every file.
- [x] 6.2 Move staged probes/logs from `%LOCALAPPDATA%\Temp\g4-staging\`
      into `tests/evidence/g4-production-gates/` (targeted/unit/full/
      negctl logs + EVIDENCE.json with case_map, known_gaps, honest
      timing), final tracked state.
      DELIVERED 2026-09-26: 10 LF-normalized logs (targeted/unit/full/
      negctl/vocabulary/diffcheck/deploy_gate/docker/docker-live/
      formal_eval_blocked) + EVIDENCE.json (HEAD, branch, contract SHA,
      peeled baseline tag, per-log SHA-256+command+exit+counts/duration,
      case_map, docker_status, eval_status, known_gaps,
      failures_and_corrections, mutation_restore, strict all_green
      definition that EXCLUDES formal EVAL and bedrock_credential).
      Arithmetic reconciled: full 938 = unit 926 + negctl 12.
      evidence-integrity test green against the staged copy (working-file
      fallback — blob-integrity attested post-commit in 6.4).
      compose config RENDER excluded from the pack (it echoes
      interpolated values); throwaway secret scrubbed and re-verified
      0 hits.
- [x] 6.3 Full runs: targeted matrix -> `tests/unit` -> full suite ->
      negctl (serial, no parallel) -> vocabulary -> docker/EVAL live
      probes; EXIT markers captured; report numbers == raw stdout.
      DELIVERED 2026-09-26 on the frozen tree (HEAD 8265a5b + the 8
      uncommitted G4 paths), strictly serial, staging dir
      `%LOCALAPPDATA%\Temp\g4-staging\20260926T212620Z`:
      targeted 177 passed/44.51s EXIT 0; unit **926 passed/103.07s**
      EXIT 0; full **938 passed/438.70s** EXIT 0; negctl **12
      passed/338.78s** EXIT 0; vocabulary PASS EXIT 0; diff --check
      clean EXIT 0; contract SHA b92e53f4…fe639 unchanged; baseline tag
      peeled 5bf299adb1e0… unchanged; deploy_gate EXIT 1 (auth_secret
      FAIL length 0 — no secret supplied on purpose, never faked;
      bedrock_credential BLOCKED; worktree_clean FAIL = the frozen
      pre-commit state; docker L3 PASS; wheelhouse PASS); docker probe
      compose config EXIT 0 (first driver pass logged a misleading
      EXIT 1 from a sourcing bug — probe re-run alone on the IDENTICAL
      frozen tree, documented in EVIDENCE.json, not papered over);
      formal EVAL `cases=30 passed=0 failed=0 blocked=30` EXIT 1 with
      runner SHA 6285b782… unchanged (--output to staging so the
      tracked runtime-eval pack stays byte-frozen).
- [x] 6.4 Evidence-BODY commit (tests/devlog/tasks), then verify against
      final Git blobs (+ a no-.git harness copy + a
      bundle->temp-clone). Then an ATTESTATION commit ticking
      verification-class boxes by exact verified SHA. Bundle kept
      OUTSIDE the repo at `D:\PlanPilot_backups\` with a `#`-commented
      `.sha256` sidecar.
      ATTESTED 2026-09-26 against evidence-body commit
      **`1d4c94bf137c133b1e3c6e730e4ec4d4a37ac66e`**:
      (a) `git status --porcelain` empty + `git diff --check` clean at
      the body commit; (b) all 10 evidence logs re-hashed FROM GIT BLOBS
      (`git cat-file blob HEAD:…`) == EVIDENCE.json recorded SHA-256,
      10/10 match, zero CRLF in any blob; (c) `git ls-files --eol`:
      every pack file i/lf w/lf attr/text eol=lf; (d) no-.git harness
      copy (`git archive HEAD` → temp): evidence-integrity test PASSES
      on working-file bytes; (e) bundle
      `D:\PlanPilot_backups\planpilot_g4_1d4c94b_20260926.bundle`
      (1,182,391 bytes, sha256
      ee8062f8c7229506181956d4669baf8124c3e78eb2cd8db67d4837ca27c7f05b)
      `git bundle verify`: complete history, HEAD 1d4c94b; (f) clone
      from the bundle to temp: HEAD/branch/contract SHA
      b92e53f4…fe639/baseline peeled 5bf299adb1e0…/11 evidence files
      all match, evidence-integrity PASSES IN THE CLONE, blob SHAs
      10/10; temp clone+harness deleted, bundle RETAINED; (g) post-
      commit deploy_gate re-run: worktree_clean flipped FAIL→**PASS**
      (the pack's FAIL was the documented frozen pre-commit state);
      auth_secret FAIL (length 0, no secret supplied — never faked),
      bedrock BLOCKED, docker L3 PASS, wheelhouse PASS, exit 1 honest.
      A FINAL bundle covering this attestation commit ships per the
      closeout instruction (Stage 10).

- [x] 6.5 **Reviewer Gate 3**: present; spec/commit only after approval.
      **APPROVED 2026-09-27 by an independent reviewer** on `d1c73e0`.
      The reviewer treated every claim in the handoff report as
      unverified and re-derived it: working tree clean, `git diff --check`
      clean, contract SHA `b92e53f4…fe639` unchanged, baseline tag
      `5bf299a…4ff56` unmoved, 33/35 with only this gate + the final
      sign-off open, bundle complete history with HEAD `d1c73e0`, bundle
      SHA-256 `c74d8962c9bab404c016874b9861a628e966dba11aa142cee4e134233e8148cc`,
      all 10 evidence-log hashes matching, evidence-integrity +
      deployment-static + startup-config targeted = **77 passed**, new G4
      negctl **2 passed** (caught=2 escaped=0 broken=0 held=2), closed
      vocabulary PASS, and an independent full-suite re-run of
      **938 passed in 515.52s**. Docker was independently REPLAYED under a
      separate project name rather than trusting the committed logs:
      compose config both profiles PASS, offline build from current source
      PASS, container up PASS, `/health/live` 200, `/health/ready` 200 with
      `db=ok`, `/health/deep` 403 unauthenticated, `/backups` write refused
      by the read-only mount, Docker HEALTHCHECK `healthy`, ready 200 again
      after restart, and all temp containers/networks/volumes/review image
      cleaned up. (The reviewer's first replay read Docker health
      immediately after app-ready and saw `starting` — a probe that did not
      wait for the HEALTHCHECK interval; corrected to await `healthy`, the
      same frozen tree passed in full. Not a product defect.) Reviewer P3,
      non-blocking: `docker.log` / `docker-live.log` carry a null top-level
      `exit_code` in EVIDENCE.json although the logs contain per-step
      `COMPOSE_CONFIG_EXIT=0` / `UP_EXIT=0` / `DOWN_EXIT=0`; future packs
      should set those two top-level values to `0` explicitly. The attested
      pack at `d1c73e0` is deliberately left byte-identical so the
      reviewer-verified hashes stay valid.

## Completion definition

- [x] All 8 clock_session / audit / backup / recovery / config / health /
      docker / EVAL facts re-anchored to HEAD; P0-1 (rw ready + no
      clock-write readiness), P0-2 (zero-HTTP recovery, offline CLI),
      P0-3 (intent-ledger + 6-point kill matrix) resolved and test-pinned.
      RE-ANCHORED 2026-09-26 at the closeout freeze: docker facts now
      live-verified (Dockerfile/compose bytes + the container acceptance
      chain, evidence pack docker.log/docker-live.log); the other seven
      families re-pinned by the targeted matrix (177 passed) at the same
      HEAD.
- [x] Docker gate honestly reported (BLOCKED if docker absent — not
      checked-and-claimed-done). Formal EVAL still BLOCKED with evidence.
      2026-09-26: docker was PRESENT and the full acceptance chain ran
      for real (deploy_gate docker L3 PASS: daemon 29.8.0 + compose
      config both profiles; container build/up/probes/ro-mount/
      healthcheck/restart-persistence all green in docker-live.log).
      Formal EVAL: `cases=30 passed=0 failed=0 blocked=30` EXIT 1,
      runner bytes unchanged (formal_eval_blocked.log).
- [x] Unit/full/negctl green; contract & baseline untouched; no
      self-modifying verifier; G4 closed only after reviewer sign-off.
      All four clauses hold at the signoff commit. Green: the reviewer
      independently re-ran the full suite on the frozen tree `d1c73e0`
      (**938 passed in 515.52s**, exit 0) rather than accepting the
      committed `full.log`, and separately confirmed targeted 77 passed,
      new G4 negctl 2 passed (caught=2 escaped=0 broken=0 held=2), and
      closed vocabulary PASS — so unit/full/negctl green is
      third-party-verified, not self-reported. Untouched: contract SHA
      `b92e53f4…fe639` and baseline tag peel `5bf299a…4ff56` both
      re-derived by the reviewer as unchanged/unmoved. No self-modifying
      verifier: no verifier, evidence log, or frozen-contract byte was
      edited to make a gate pass; the only test-file changes this round
      are the new 5.3 static regressions and the new G4 negctl, and the
      one semantics correction in `test_preflight_*` was forced by a real
      product defect (read-only `/backups` mount vs. a preflight
      writability demand) recorded in
      EVIDENCE.json `failures_and_corrections`. Signed off: 6.5 now
      carries the reviewer's own APPROVED record, so this item is ticked
      *after* sign-off, never before. Formal EVAL stays
      `0 PASS / 30 BLOCKED` (exit 1) and is explicitly OUTSIDE this local
      G4 completion claim — it belongs to the later real AWS/Bedrock
      phase and must not be rewritten because G4 closed.
