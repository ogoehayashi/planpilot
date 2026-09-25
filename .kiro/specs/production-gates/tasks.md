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

- [ ] 4.1 `verify_audit_connection(conn)` pure SELECT-only over the
      connection (no ctor, no repair); hash = `sha256((prev +
      row["record"]).encode())` on the STORED record STRING; re-canonicalize
      mutation must be rejected. AST guard: verifier source has no
      CREATE/INSERT/UPDATE/DELETE.
- [ ] 4.2 `verify_backup_file(path)->BackupVerification` — a SEPARATE
      read-only fn from `verify_audit_connection`: `mode=ro&immutable=1`
      + `query_only=ON`; requires `PRAGMA integrity_check == "ok"`;
      REFUSES if a `-wal`/`-shm` sibling exists beside the backup; core
      table presence (audit_chain, audit_chain_head, clock_session,
      security_events, decision_traces, publication_receipt,
      authority_state, idempotency_registry, factory_states); audit
      chain+head walk; backup SHA unchanged before/after.
- [ ] 4.3 `backup_database()` STAYS `-> None` (design §5 V3 — no
      public API change); `tools/scheduled_backup.py` derives the
      deterministic backup filename from its own inputs, computes its
      SHA-256, then calls verify_backup_file on it AFTER each backup, excludes failures from
      the retention "successful" set, and atomically writes
      `last_verified_backup.json` (manifest binding path + sha256);
      deep-health age reads THIS manifest.
- [ ] 4.4 OS lock helper (msvcrt/flock) acquired by the ENTRY POINT
      BEFORE any Database/Clock construction (round-4 P0-2 boot order:
      env -> parse+non-DB preflight -> LOCK -> marker/receipt/ACK gate
      -> Clock -> Database -> services -> bind; shutdown: HTTP -> DB
      close -> release lock LAST), held for life; restore takes it
      NON-blocking (fail immediately = "server still holds it");
      pidfile advisory only.
- [ ] 4.5 Intent-ledger state machine (`<db>.restore-state`): every op
      logged pending/done with per-file SHA; a phase advances ONLY when
      all its ops are done; on reopen `reconcile_ledger()` re-derives
      fact from filesystem (main.db vs staging vs quarantine + SHA),
      never trusts the label. WAL/SHM moved file-by-file. `--rollback`
      path. Receipt temp->fsync->replace->parent-fsync binding backup
      SHA+target+audit_head+quarantine generation; any later byte =>
      gate rejects.
- [ ] 4.6 Zero-HTTP recovery gate (P0-2): NO unacked server starts
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
- [ ] 4.7 unit: kill-point matrix at SIX points (after marker-pre-stage,
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
      EVAL boundary). tests/unit/test_deploy_gate.py.
- [ ] 5.3 Docker: Dockerfile HEALTHCHECK switches to `/health/ready`;
      compose api mounts backups read-only (deep age). **Docker is a G4
      acceptance item: docker absent => G4 stays BLOCKED; do NOT tick
      5.3 as G4-complete.** Only AWS/formal-EVAL are permitted non-goal
      BLOCKED edges.
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

- [ ] 6.1 negctl sandbox adds must-fail mutations, EXACT counts pinned
      HERE only: ready ro-probe false-green; restore skips verification;
      ack gate bypass; secret gate bypass; hollow same-name trigger
      accepted; [hold] clock/publisher pass.
- [ ] 6.2 Move staged probes/logs from `%LOCALAPPDATA%\Temp\g4-staging\`
      into `tests/evidence/g4-production-gates/` (targeted/unit/full/
      negctl logs + EVIDENCE.json with case_map, known_gaps, honest
      timing), final tracked state.
- [ ] 6.3 Full runs: targeted matrix -> `tests/unit` -> full suite ->
      negctl (serial, no parallel) -> vocabulary -> docker/EVAL live
      probes; EXIT markers captured; report numbers == raw stdout.
- [ ] 6.4 Evidence-BODY commit (tests/devlog/tasks), then verify against
      final Git blobs (+ a no-.git harness copy + a
      bundle->temp-clone). Then an ATTESTATION commit ticking
      verification-class boxes by exact verified SHA. Bundle kept
      OUTSIDE the repo at `D:\PlanPilot_backups\` with a `#`-commented
      `.sha256` sidecar.

- [ ] 6.5 **Reviewer Gate 3**: present; spec/commit only after approval.

## Completion definition

- [ ] All 8 clock_session / audit / backup / recovery / config / health /
      docker / EVAL facts re-anchored to HEAD; P0-1 (rw ready + no
      clock-write readiness), P0-2 (zero-HTTP recovery, offline CLI),
      P0-3 (intent-ledger + 6-point kill matrix) resolved and test-pinned.
- [ ] Docker gate honestly reported (BLOCKED if docker absent — not
      checked-and-claimed-done). Formal EVAL still BLOCKED with evidence.
- [ ] Unit/full/negctl green; contract & baseline untouched; no
      self-modifying verifier; G4 closed only after reviewer sign-off.
