"""G4 A4 sentinel: the clock/health defences must be observably load-bearing.

SIX disposable-copy mutations (never in the normal suite — an
intentional red-main would lie about CI):
  1. strip the three CREATE TRIGGER clock_session blocks from
     persistence.py => the tamper tests must fail (defence gone).
  2. revert clock._persist's fail-closed raise to the old
     return-stamp fail-open => the row-missing test must fail.
  3. (G4 1.0.1-#3) neuter the shape-guard comparison to pass-through
     => the hollow-pre-plant digest test must fail (a no-op shell
     would open happily).
  4. (G4 Phase 3) remove _probe_ready's BEGIN IMMEDIATE reservation
     => the real-writer-lock test must fail: readiness that stays
     green while a writer owns the DB is the false green the split
     exists to kill.
  5. (G4 Phase 3) drop the in-txn CREATE falsifier => the read-only
     FILE test must fail: empirically sqlite accepts BEGIN IMMEDIATE
     on a chmod-444 db and denies only the write (rev.3 P0-1 shape,
     pinned probed on Windows this batch).
  6. (Batch A P1-1) revert the ready probe to plain sqlite3.connect
     => the missing-file test must fail: plain connect CREATES a
     zero-byte phantom DB and false-greens readiness for a database
     that never existed (reviewer-reproduced).
Each mutation runs its pinned focused subset in a sandbox subprocess;
the sentinel additionally requires a REAL test-failure line in stdout
(a collection/import error is `broken`, not `caught` — no wrong-reason
self-deception). The real tree is byte-restored and hash-checked
afterwards, exactly like the security-audit negctl file.
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
FOCUSED = ("tests/unit/test_clock_session_defence.py",
           "tests/unit/test_plan_store_persistence.py")
SUBJECTS = ("src/planpilot/persistence.py", "src/planpilot/clock.py",
            "tools/api_server.py")

_TRIP = '''            CREATE TRIGGER IF NOT EXISTS clock_session_no_delete
            BEFORE DELETE ON clock_session BEGIN
                SELECT RAISE(ABORT, 'clock_session is anchored; delete is forbidden');
            END;
            CREATE TRIGGER IF NOT EXISTS clock_session_anchor_immutable
            BEFORE UPDATE OF scenario_anchor, real_wall_started_at ON clock_session BEGIN
                SELECT RAISE(ABORT, 'clock_session anchors are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS clock_session_no_rewind
            BEFORE UPDATE OF last_issued_scenario_time ON clock_session
            WHEN NEW.last_issued_scenario_time < OLD.last_issued_scenario_time BEGIN
                SELECT RAISE(ABORT, 'clock_session time may not rewind');
            END;
'''

MUTATIONS = (
    ("clock_session triggers stripped", "src/planpilot/persistence.py",
     _TRIP, "", FOCUSED),
    ("_persist fail-open restored", "src/planpilot/clock.py",
     """            if row is None:
                # G4 A1 (design §2): the anchor row is GONE. Honest flow
                # cannot reach here — attach_database INSERTs the singleton
                # BEFORE binding _db — so this is tamper (delete bypassing
                # the trigger, or a hand-carved file). Fail CLOSED: never
                # hand back an un-persisted stamp as if it were durable.
                raise RuntimeError(
                    "clock session row missing — refusing to issue "
                    "un-persisted scenario time")
""",
     """            if row is None:
                return stamp
""", FOCUSED),
    ("shape guard neutered to pass-through", "src/planpilot/persistence.py",
     """            if stored is None or _sql_fingerprint(stored[0]) != _sql_fingerprint(canonical_sql):
""",
     """            if False:  # NEGCTL MUTATION: digest check bypassed
""", FOCUSED),
    ("ready probe loses its writer reservation", "tools/api_server.py",
     """            conn.execute("BEGIN IMMEDIATE")
""",
     """            pass  # NEGCTL MUTATION: no writer reservation at all
""",
     ("tests/unit/test_health_split.py::"
      "test_ready_503_under_real_writer_lock_then_200_after_release",)),
    ("ready probe drops the in-txn write falsifier", "tools/api_server.py",
     """            conn.execute("CREATE TABLE _pp_ready_probe(x)")
""",
     """            conn.execute("SELECT 1")  # NEGCTL MUTATION: ro-file false green
""",
     ("tests/unit/test_health_split.py::"
      "test_ready_503_on_readonly_file_never_false_green",)),
    ("ready probe reverts to plain connect (phantom-DB false green)",
     "tools/api_server.py",
     """            uri = Path(path).resolve().as_uri() + "?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=budget)
""",
     """            conn = sqlite3.connect(path, timeout=budget)  # NEGCTL MUTATION
""",
     ("tests/unit/test_health_split.py::"
      "test_ready_503_when_db_file_missing_and_never_creates_it",)),
)


def once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"[{label}] broken anchor: {count} occurrences")
    return text.replace(old, new)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    target = tmp_path_factory.mktemp("clock-defence-negctl") / "repo"
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".venv*", "__pycache__", "*.pyc", "tests/evidence"),
    )
    return target


def test_clock_defence_mutations_are_caught_and_real_tree_unchanged(sandbox):
    before = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SUBJECTS}
    env = dict(os.environ, PYTHONPATH=str(sandbox / "src"))
    caught = escaped = broken = 0
    for label, relative, old, new, focused in MUTATIONS:
        target = sandbox / relative
        pristine_bytes = target.read_bytes()
        pristine = pristine_bytes.decode("utf-8")
        try:
            target.write_bytes(once(pristine, old, new, label).encode("utf-8"))
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *focused, "-q", "--no-header", "-p", "no:cacheprovider"],
                cwd=sandbox, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
            if result.returncode == 0:
                escaped += 1
                pytest.fail(f"mutation escaped: {label}")
            # caught must mean REAL test failures, not a crashed import or
            # collection error (which would "fail" for the wrong reason).
            failed = re.findall(r"^FAILED (\S+)", result.stdout, re.M)
            if not failed:
                broken += 1
                pytest.fail(f"mutation {label}: run failed with no FAILED "
                            f"line (wrong-reason catch): "
                            f"{result.stdout[-500:]}{result.stderr[-500:]}")
            caught += 1
        except AssertionError:
            broken += 1
            raise
        finally:
            target.write_bytes(pristine_bytes)
    after = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SUBJECTS}
    assert before == after
    assert all((sandbox / p).read_bytes() == (ROOT / p).read_bytes() for p in SUBJECTS)
    print(f"CLOCK/HEALTH DEFENCE NEGATIVE CONTROL | caught={caught} escaped={escaped} broken={broken} of {len(MUTATIONS)}")
