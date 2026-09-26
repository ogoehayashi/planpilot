"""G4 closeout negative control (tasks 6.1, design §8) — the two §8
must-fail lines NOT already pinned elsewhere, plus the two required holds.

design §8 enumerates five must-fail mutations + one hold pair. Three of the
five ALREADY have a real, sandboxed negative control — this file REFERENCES
them instead of re-staging duplicate theater (tasks 6.1 rule "如当前恢复负控
已有相同防线，应合并或引用"):

  §8.1 ready probe false-green (ro/reservation-only/phantom-DB)  ->
        tests/negative_control/test_clock_defence_negctl.py mutations 4,5,6
  §8.3 ack/marker/receipt gate bypass                            ->
        tests/negative_control/test_recovery_negctl.py mutations 3,4,5
  §8.5 hollow same-name clock trigger accepted                   ->
        tests/negative_control/test_clock_defence_negctl.py mutation 3

The two lines with NO existing control are added HERE:

  §8.2 restore skips the read-only backup/audit verification battery ->
        M1 flips plan_restore's `if not v.ok` gate; a bare (non-restorable)
        Database backup would then be accepted. must-fail:
        test_restore_ledger.py::test_bare_database_backup_is_not_restorable
  §8.4 production secret / startup gate short-circuited ->
        M2 flips parse_startup_env's `if len(secret) < _SECRET_MIN`; a
        <32-char secret would then parse. must-fail:
        test_startup_config.py::test_secret_min_32_both_envs

HOLDS (tasks 6.1: "clock 防线在安全变异下仍保持预期; publisher 已审计事务
防线保持不退化"). Each mutation above is SURGICAL — it touches restore.py /
startup_config.py only. The hold suites assert that, under those mutations,
the clock_session defence AND the publisher audited-transaction schema stay
GREEN: a G4-closeout mutation must never have collateral damage on the
G1/G2 audited core. Both hold files are socket-free (asserted structurally
by test_hold_suites_are_http_free below), fast, and explicit — never
`expected_suite=None` (which would drag in the whole unit suite and ride the
hold verdict on local-HTTP timing, the p2-7 false-escape class).

Sandbox discipline (same as the clock/recovery files): mutations are applied
to a repo COPY outside .git; the REAL tree files are hash-checked before and
after and proven byte-identical; a hard failure never leaves a mutation in
the working source.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Socket-free hold files: the clock_session defence (G4 Phase 1) and the
# publisher audited-transaction schema (G2). Neither opens a socket.
HOLD = ("tests/unit/test_clock_session_defence.py",
        "tests/unit/test_publisher_schema.py")

# --- §8.2: restore skips the read-only battery -------------------------------
_BATTERY_OLD = (
    '    v = verify_backup_file(backup)\n'
    '    if not v.ok:\n'
    '        raise RestoreAborted(\n'
    '            f"backup refused by the read-only battery BEFORE any intent "\n'
    '            f"was written (never restorable):\\n{v.failure_text()}")\n')
_BATTERY_NEW = (
    '    v = verify_backup_file(backup)\n'
    '    if False:  # NEGCTL MUTATION: battery skipped, any backup accepted\n'
    '        raise RestoreAborted(\n'
    '            f"backup refused by the read-only battery BEFORE any intent "\n'
    '            f"was written (never restorable):\\n{v.failure_text()}")\n')

# --- §8.4: production startup secret gate short-circuited --------------------
_SECRET_OLD = '    if len(secret) < _SECRET_MIN:\n'
_SECRET_NEW = '    if False:  # NEGCTL MUTATION: secret-length gate bypassed\n'

# Each row: (label, relative_path, old, new, must_fail_focused, hold_suite)
MUTATIONS = (
    ("restore skips the read-only verification battery (§8.2)",
     "src/planpilot/restore.py", _BATTERY_OLD, _BATTERY_NEW,
     ("tests/unit/test_restore_ledger.py::"
      "test_bare_database_backup_is_not_restorable",), HOLD),
    ("production startup secret gate short-circuited (§8.4)",
     "src/planpilot/startup_config.py", _SECRET_OLD, _SECRET_NEW,
     ("tests/unit/test_startup_config.py::test_secret_min_32_both_envs",),
     HOLD),
)

SUBJECTS = tuple(sorted({m[1] for m in MUTATIONS}))


def once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"[{label}] broken anchor: {count} occurrences")
    return text.replace(old, new)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    target = tmp_path_factory.mktemp("g4-closeout-negctl") / "repo"
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".venv*",
                                      "__pycache__", "*.pyc",
                                      "tests/evidence"),
    )
    return target


def _run(sandbox, focused):
    env = dict(os.environ, PYTHONPATH=str(sandbox / "src"))
    env.pop("PYTHONUTF8", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *focused, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=sandbox, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)


def test_g4_closeout_mutations_caught_and_holds_and_tree_unchanged(sandbox):
    """must-fail caught for a REAL test-failure reason; every hold stays
    green; the real tree is byte-identical before and after."""
    before = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
              for p in SUBJECTS}
    caught = escaped = broken = 0
    held = []
    for label, relative, old, new, focused, hold in MUTATIONS:
        target = sandbox / relative
        pristine_bytes = target.read_bytes()
        pristine = pristine_bytes.decode("utf-8")
        try:
            target.write_bytes(once(pristine, old, new, label).encode("utf-8"))
            # (1) must-fail: the focused defence test MUST fail, for a real
            #     FAILED reason (not a crashed import / collection error).
            res = _run(sandbox, focused)
            if res.returncode == 0:
                escaped += 1
                pytest.fail(f"mutation escaped: {label}")
            failed = re.findall(r"^FAILED (\S+)", res.stdout, re.M)
            if not failed:
                broken += 1
                pytest.fail(f"mutation {label}: run failed with no FAILED "
                            f"line (wrong-reason catch): "
                            f"{res.stdout[-500:]}{res.stderr[-500:]}")
            caught += 1
            # (2) hold: the clock + publisher audited-core suites must STAY
            #     GREEN under this mutation (it is surgical, no collateral).
            hres = _run(sandbox, hold)
            if hres.returncode != 0:
                escaped += 1
                pytest.fail(f"hold regressed under [{label}]: expected the "
                            f"clock/publisher defences to hold, but:\n"
                            f"{hres.stdout[-800:]}")
            held.append(label)
        except AssertionError:
            broken += 1
            raise
        finally:
            target.write_bytes(pristine_bytes)
    after = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
             for p in SUBJECTS}
    assert before == after, "negative control changed the REAL working tree"
    assert all((sandbox / p).read_bytes() == (ROOT / p).read_bytes()
               for p in SUBJECTS), "sandbox source not restored"
    # EXACT counts (tasks 6.1: pinned here, derived from this run's stdout).
    assert caught == 2, f"caught {caught}, expected 2"
    assert escaped == 0 and broken == 0
    assert len(held) == 2, f"held {len(held)}, expected 2"
    print(f"G4 CLOSEOUT NEGATIVE CONTROL | caught={caught} escaped={escaped} "
          f"broken={broken} held={len(held)} of {len(MUTATIONS)}")


def test_hold_suites_are_http_free():
    """Structural guard (p2-7 lesson): a hold suite runs inside the mutation
    loop, so a hold verdict must never ride on local-HTTP socket timing. Every
    file named in a hold suite must contain none of the known network entry
    points. Keyword tripwire, not a proof of total network absence — it stops
    the usual ways a flaky file creeps in and fails here in seconds."""
    banned = ("urlopen", "http.server", "HTTPServer", "socket", "Request(",
              "requests", "httpx", "aiohttp", "socketserver", "urllib")
    hold_files = {f for m in MUTATIONS for f in m[5]}
    assert hold_files, "the hold suites vanished from MUTATIONS"
    offenders = {}
    for name in sorted(hold_files):
        text = (ROOT / name).read_text(encoding="utf-8")
        hits = [b for b in banned if b in text]
        if hits:
            offenders[name] = hits
    assert not offenders, f"hold suites must be socket-free: {offenders}"
