"""G4 Phase-4 negative control — every Phase-4 defence line flipped red.

Mutations (each flips ONE enforcement line, focused tests MUST fail
for a REAL test-failure reason — not a crashed import):

1. V3 "verify after create" bypassed (scheduled_backup: battery result
   ignored, unverified file advertised as verified). 4.3 devlog debt.
2. Boot-order lock moved AFTER the Database construction (api_server):
   the denied-lock world must construct NOTHING — the ordering test
   fails for the right reason.
3. Recovery gate stripped from main() (unacked restore would silently
   serve): the gate-denied startup test must fail.
4. Gate stops binding receipt bytes (receipt_sha256 comparison dropped
   — receipt edits would boot): the byte-edit ack-void test must fail.
5. ack-invalidate removed (a second restore would inherit the first
   ack): the same-backup-voids-old-ack test must fail.

Same sandbox discipline as the clock-defence file: mutations applied
to a repo COPY outside .git, real tree byte-restored and hash-checked.
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

SCHEDULED = ("tests/unit/test_scheduled_verified_set.py::"
             "test_refused_backup_is_quarantined_never_advertised",)
BOOT = ("tests/unit/test_db_lock_boot.py::test_lock_precedes_every_writer",
        "tests/unit/test_db_lock_boot.py::test_lock_released_last_on_shutdown")
GATE = ("tests/unit/test_kill_matrix.py::test_gate_denied_startup_constructs_nothing",)
ACKVOID = ("tests/unit/test_restore_ledger.py::test_receipt_byte_edit_voids_ack",)
ACKINVAL = ("tests/unit/test_restore_ledger.py::test_second_restore_of_same_backup_voids_old_ack",)

MUTATIONS = (
    ("verify-after-create bypassed (V3)", "tools/scheduled_backup.py",
     """    result = verify_backup_file(target)
    if not result.ok:
""",
     """    result = verify_backup_file(target)
    if False:  # NEGCTL MUTATION: battery result ignored, advertised blind
""", SCHEDULED),
    ("boot order: lock taken AFTER Database", "tools/api_server.py",
     """    try:
        db_lock = acquire_server_lock(cfg.db_path)
""",
     """    db_lock = None
    try:
        _ = Database(cfg.db_path)  # NEGCTL MUTATION: writer BEFORE lock
""", BOOT),
    ("recovery gate stripped from startup", "tools/api_server.py",
     """        gate_ok, gate_reasons = evaluate_recovery_gate(cfg.db_path)
""",
     """        gate_ok, gate_reasons = (True, [])  # NEGCTL MUTATION: gate removed
""", GATE),
    ("gate ignores receipt file bytes", "src/planpilot/restore.py",
     """    checks = (("receipt_sha256", adata.get("receipt_sha256"), receipt_sha),
""",
     """    checks = (("receipt_sha256", adata.get("receipt_sha256"), adata.get("receipt_sha256")),  # NEGCTL MUTATION
""", ACKVOID),
    ("ack-invalidate op removed from ledger", "src/planpilot/restore.py",
     """    ack = ack_path(Path(ledger["target_db"]))
    if ack.exists():
        ack.unlink()
        _fsync_dir(ack.parent)
""",
     """    # NEGCTL MUTATION: old ack survives a new restore
    return
""", ACKINVAL),
)

SUBJECTS = tuple(sorted({m[1] for m in MUTATIONS}))


def once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"[{label}] broken anchor: {count} occurrences")
    return text.replace(old, new)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    target = tmp_path_factory.mktemp("recovery-negctl") / "repo"
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".venv*",
                                      "__pycache__", "*.pyc",
                                      "tests/evidence"),
    )
    return target


def test_recovery_defence_mutations_are_caught_and_real_tree_unchanged(sandbox):
    before = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
              for p in SUBJECTS}
    env = dict(os.environ, PYTHONPATH=str(sandbox / "src"))
    caught = escaped = broken = 0
    for label, relative, old, new, focused in MUTATIONS:
        target = sandbox / relative
        pristine_bytes = target.read_bytes()
        pristine = pristine_bytes.decode("utf-8")
        try:
            target.write_bytes(once(pristine, old, new, label).encode("utf-8"))
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *focused, "-q",
                 "--no-header", "-p", "no:cacheprovider"],
                cwd=sandbox, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
            if result.returncode == 0:
                escaped += 1
                pytest.fail(f"mutation escaped: {label}")
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
    after = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
             for p in SUBJECTS}
    assert before == after
    assert all((sandbox / p).read_bytes() == (ROOT / p).read_bytes()
               for p in SUBJECTS)
    print(f"RECOVERY DEFENCE NEGATIVE CONTROL | caught={caught} "
          f"escaped={escaped} broken={broken} of {len(MUTATIONS)}")
