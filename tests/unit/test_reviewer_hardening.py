"""Reviewer hardening round — the six acceptance proofs, one per claim.

P0: an existing marker IS an interrupted restore's ledger. A second
restore with a DIFFERENT backup is refused with zero writes; the same
world still converges under the ORIGINAL backup (resume) and still
rolls back byte-exact. P1s: rollback/ack take the OS lock before any
sidecar read; random corrupt bytes and BLOB/NULL audit records return
STRUCTURED failures (SHA unchanged, CLI exit 1 without a traceback);
the verified manifest may only bless the exact bytes the battery
actually verified.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from planpilot import restore as R
from planpilot.audit import (
    AuditChainError, AuditTrail, verify_audit_connection,
)
from planpilot.backup import backup_database, sha256_file, verify_backup_file
from planpilot.db_lock import DbLockHeld, acquire_server_lock
from planpilot.factory_state import FactoryStateRegistry
from planpilot.persistence import Database
from planpilot.restore import (
    RestoreAborted, ack_path, marker_path, quarantine_root, receipt_path,
    restore_database, rollback_ledger, write_ack,
)

ROOT = Path(__file__).resolve().parents[2]

RUNNER = (
    "import sys\n"
    "sys.path.insert(0, 'src')\n"
    "from planpilot.restore import restore_database\n"
    "restore_database(sys.argv[1], sys.argv[2])\n"
    "print('DONE', flush=True)\n"
)


def _runtime_db(path: Path) -> Database:
    db = Database(path)
    AuditTrail(db)
    FactoryStateRegistry(db)
    for i in range(3):
        db.audit("plan-hard", "system", f"event_{i}", {"seq": i})
    db.conn.commit()
    return db


def _subenv():
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    return env


import json


def _kill_at(live: Path, backup: Path, op: str):
    env = dict(os.environ, PLANPILOT_RESTORE_KILL_AT=op)
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER, str(live), str(backup)],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=90)
    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert "DONE" not in proc.stdout
    return proc


@pytest.fixture
def p0_world(tmp_path):
    """live DB + backup A, live moves on + backup B; then a restore of
    A is hard-killed with main ALREADY in quarantine (reviewer's exact
    repro: marker + first-generation quarantine on disk, kill at
    quarantine-wal)."""
    live = tmp_path / "main.db"
    db = _runtime_db(live)
    bk_a = tmp_path / "bk_a.db"
    backup_database(live, bk_a)
    db.close()
    db2 = _runtime_db(live)
    db2.audit("plan-hard", "system", "event_post_a", {"seq": 77})
    db2.conn.commit()
    bk_b = tmp_path / "bk_b.db"
    backup_database(live, bk_b)
    db2.close()
    # a CLOSED sqlite DB checkpoints its WAL away, so the trio-move
    # seams need FORGED siblings — kill at quarantine-wal requires the
    # -wal op to actually exist on disk (same trick as kill_matrix).
    (tmp_path / "main.db-wal").write_bytes(b"WALFORGED-1")
    (tmp_path / "main.db-shm").write_bytes(b"SHMFORGED-1")
    pre_main = sha256_file(live)
    _kill_at(live, bk_a, "quarantine-wal")
    assert marker_path(live).exists()
    qroot = quarantine_root(live)
    assert any(p.is_dir() for p in qroot.iterdir())   # gen-1 quarantine live
    return {"live": live, "bk_a": bk_a, "bk_b": bk_b, "tmp": tmp_path,
            "pre_main": pre_main}


def _safe_sha(p: Path):
    return sha256_file(p) if p.is_file() else None


def _fingerprint_world(w):
    """Every byte a refusal MUST leave untouched: marker, target trio,
    all quarantine generation trees."""
    live = w["live"]
    marker = marker_path(live)
    qroot = quarantine_root(live)
    tree = {}
    if qroot.is_dir():
        for gen in sorted(qroot.iterdir()):
            if gen.is_dir():
                for f in sorted(gen.iterdir()):
                    tree[gen.name + "/" + f.name] = _safe_sha(f)
    return {"marker": marker.read_bytes() if marker.is_file() else None,
            "trio": [_safe_sha(live),
                     _safe_sha(Path(str(live) + "-wal")),
                     _safe_sha(Path(str(live) + "-shm"))],
            "quarantine": tree}


# ---- P0 acceptance: mismatched second restore refused, zero writes
def test_p0_second_restore_other_backup_refused_zero_writes(p0_world):
    w = p0_world
    before = _fingerprint_world(w)
    with pytest.raises(RestoreAborted,
                       match="resume it with the SAME backup"):
        restore_database(w["live"], w["bk_b"])
    assert _fingerprint_world(w) == before


def test_p0_alien_target_marker_never_rebranded(p0_world):
    """A marker naming a DIFFERENT target is foreign property: refused
    before any backup comparison, zero writes."""
    w = p0_world
    marker = marker_path(w["live"])
    alien = {"schema_version": 1, "generation": "99999999-9999",
             "target_db": str(w["tmp"] / "somewhere-else.db"),
             "backup_path": str(w["bk_b"]), "backup_sha256": "0" * 64,
             "ops": {}, "facts": {}}
    marker.write_text(json.dumps(alien), encoding="utf-8")
    before = _fingerprint_world(w)
    with pytest.raises(RestoreAborted, match="different target"):
        restore_database(w["live"], w["bk_b"])
    assert _fingerprint_world(w) == before


# ---- same world still converges under the ORIGINAL backup (resume)
def test_p0_resume_with_original_backup_converges(p0_world):
    w = p0_world
    ledger = restore_database(w["live"], w["bk_a"])
    assert sha256_file(w["live"]) == ledger["backup_sha256"]
    assert not marker_path(w["live"]).exists()
    assert ledger["ops"]["receipt-write"] == "done"
    data = json.loads(receipt_path(w["live"]).read_text(encoding="utf-8"))
    assert data["generation"] == ledger["generation"]


# ---- and --rollback still returns the original bytes
def test_p0_rollback_still_works_after_refusal(p0_world):
    w = p0_world
    with pytest.raises(RestoreAborted, match="resume it with the SAME backup"):
        restore_database(w["live"], w["bk_b"])
    ledger = rollback_ledger(w["live"])
    assert sha256_file(w["live"]) == w["pre_main"]
    assert not marker_path(w["live"]).exists()
    assert ledger["facts"][str(w["live"])]["sha256"] == w["pre_main"]



# ---- P1a: recovery writers refuse AT THE LOCK, zero sidecar writes
HOLDER = (
    "import os, sys, time\n"
    "sys.path.insert(0, 'src')\n"
    "from planpilot.db_lock import acquire_server_lock\n"
    "h = acquire_server_lock(sys.argv[1])\n"
    "print('LOCKED', os.getpid(), flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
    "h.release()\n")


def _kill_tree(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.wait(timeout=10)
    else:
        p.kill()
        p.wait(timeout=10)


def test_rollback_and_ack_refuse_under_live_lock_zero_writes(p0_world):
    w = p0_world
    live = w["live"]
    before = _fingerprint_world(w)
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(live), "30"],
                         cwd=str(ROOT), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=_subenv())
    try:
        parts = p.stdout.readline().split()
        assert parts and parts[0] == "LOCKED", p.stderr.read()
        # Lock-first: BOTH raise DbLockHeld — write_ack would otherwise
        # have raised 'no receipt' (it is locked before ANY sidecar read),
        # rollback before it ever reads the marker.
        with pytest.raises(DbLockHeld):
            rollback_ledger(live)
        with pytest.raises(DbLockHeld):
            write_ack(live, "operator")
    finally:
        _kill_tree(p)
    assert _fingerprint_world(w) == before
    assert not ack_path(live).exists()          # zero writes, proven


# ---- P1b: random corrupt bytes => structured failure, never a raise
def test_random_corrupt_file_returns_structured_failure(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"THIS-IS-NOT-SQLITE" * 300)
    sha_before = sha256_file(junk)
    result = verify_backup_file(junk)          # must NOT raise
    assert result.ok is False
    names = {c.name: c for c in result.checks}
    assert names["integrity_check"].ok is False
    assert "DatabaseError" in names["integrity_check"].detail or \
           "file is not a database" in names["integrity_check"].detail
    assert names["sha_unchanged"].ok is True
    assert sha256_file(junk) == sha_before     # bytes untouched by battery


def test_cli_verify_corrupt_exits_1_without_traceback(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"THIS-IS-NOT-SQLITE" * 300)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "verify_backup.py"), str(junk)],
        capture_output=True, text=True, errors="replace",
        cwd=str(ROOT), env=_subenv(), timeout=90)
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert "integrity_check" in proc.stdout    # structured failure line


# ---- P1b/audit: BLOB-typed record => structured AuditChainError
def test_blob_typed_audit_record_fails_structured(tmp_path):
    live = tmp_path / "main.db"
    db = _runtime_db(live)
    db.close()
    bk = tmp_path / "good.db"
    backup_database(live, bk)
    tamper = sqlite3.connect(bk)
    # the backup carries the append-only triggers; a REAL external
    # corruption (filesystem editor, rogue tool) bypasses them — drop
    # them here to simulate those stored bytes.
    tamper.executescript(
        "DROP TRIGGER audit_chain_no_update;"
        "DROP TRIGGER audit_chain_no_delete;")
    tamper.execute("UPDATE audit_chain SET record=X'DEADBEEF' WHERE id=1")
    tamper.commit()
    tamper.close()
    # direct walk: structured error, no bare TypeError leaking out
    conn = sqlite3.connect(bk)
    with pytest.raises(AuditChainError) as ei:
        verify_audit_connection(conn)
    conn.close()
    assert "bytes" in str(ei.value) or "BLOB" in str(ei.value)
    result = verify_backup_file(bk)            # battery: structured too
    assert result.ok is False
    chain = [c for c in result.checks if c.name == "audit_chain"][0]
    assert chain.ok is False and "chain_break" in chain.detail


# ---- P1c: bytes mutated AFTER verification must never reach manifest
def test_post_verification_mutation_refused_manifest_untouched(tmp_path,
                                                               monkeypatch):
    import tools.scheduled_backup as SB
    live = tmp_path / "main.db"
    src = _runtime_db(live)
    src.close()
    out = tmp_path / "backups"
    real_verify = SB.verify_backup_file

    def mutating_verify(p):        # third party rewrites AFTER the battery
        result = real_verify(p)
        Path(p).write_bytes(Path(p).read_bytes() + b"TAMPERED-POST-VERIFY")
        return result

    monkeypatch.setattr(SB, "verify_backup_file", mutating_verify)
    report = SB.run(live, out, keep=3)
    assert report["backup"] is None
    assert "drift" in report["failure"]
    assert not (out / SB.MANIFEST_NAME).exists()   # manifest NEVER blessed
    moved = Path(report["quarantined"])
    assert moved.is_file() and moved.read_bytes().endswith(
        b"TAMPERED-POST-VERIFY")                  # quarantined as mutated
    # and a clean run still blesses correctly (guard not over-broad)
    monkeypatch.undo()
    report2 = SB.run(live, out, keep=3)
    assert report2["backup"] is not None
    manifest = json.loads((out / SB.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["sha256"] == sha256_file(Path(report2["backup"]))
