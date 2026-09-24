"""G4 A4 sentinel: the clock_session defence must be observably load-bearing.

Two disposable-copy mutations (never in the normal suite — an intentional
red-main would lie about CI):
  1. strip the three CREATE TRIGGER clock_session blocks from
     persistence.py => the tamper tests must fail (defence gone).
  2. revert clock._persist's fail-closed raise to the old
     return-stamp fail-open => the row-missing test must fail.
Each mutation runs a socket-free focused subset in a sandbox subprocess;
must-fail sets are pinned non-empty. The real tree is byte-restored and
hash-checked afterwards, exactly like the security-audit negctl file.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FOCUSED = ("tests/unit/test_clock_session_defence.py",
           "tests/unit/test_plan_store_persistence.py")
SUBJECTS = ("src/planpilot/persistence.py", "src/planpilot/clock.py")

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
     _TRIP, ""),
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
"""),
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
    for label, relative, old, new in MUTATIONS:
        target = sandbox / relative
        pristine_bytes = target.read_bytes()
        pristine = pristine_bytes.decode("utf-8")
        try:
            target.write_bytes(once(pristine, old, new, label).encode("utf-8"))
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *FOCUSED, "-q", "--no-header", "-p", "no:cacheprovider"],
                cwd=sandbox, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
            if result.returncode == 0:
                escaped += 1
                pytest.fail(f"mutation escaped: {label}")
            caught += 1
        except AssertionError:
            broken += 1
            raise
        finally:
            target.write_bytes(pristine_bytes)
    after = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SUBJECTS}
    assert before == after
    assert all((sandbox / p).read_bytes() == (ROOT / p).read_bytes() for p in SUBJECTS)
    print(f"CLOCK DEFENCE NEGATIVE CONTROL | caught={caught} escaped={escaped} broken={broken} of {len(MUTATIONS)}")
