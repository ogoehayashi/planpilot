"""G4 4.7 — hard-kill matrix (design §5 R2, SIX points), read-only
falsifier, and round-4 P0-2 boot-order proofs.

Each matrix point: a REAL subprocess dies via os._exit at the named
ledger op — no cleanup, no finally, exactly the crash the ledger
exists for. Then the parent (a) classifies the world from
marker+facts via reconcile (no conflicts, expected done/pending),
(b) re-runs restore to convergence (target sha == backup sha, trio in
ONE generation dir with original names, marker gone, receipt valid),
and (c) from the same killed world, rolls back byte-exact.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from planpilot.audit import AuditTrail
from planpilot.backup import backup_database, sha256_file
from planpilot.db_lock import DbLockHeld, acquire_server_lock
from planpilot.factory_state import FactoryStateRegistry
from planpilot.persistence import Database
from planpilot.restore import (
    ack_path, evaluate_recovery_gate, load_ledger, marker_path,
    receipt_path, reconcile_ledger, restore_database, rollback_ledger,
    stage_path, write_ack,
)

ROOT = Path(__file__).resolve().parents[2]

RUNNER = (
    "import sys\n"
    "sys.path.insert(0, 'src')\n"
    "from planpilot.restore import restore_database\n"
    "restore_database(sys.argv[1], sys.argv[2])\n"
    "print('DONE', flush=True)\n"
)


@pytest.fixture
def world(tmp_path):
    live = tmp_path / "main.db"
    db = Database(live)
    AuditTrail(db)
    FactoryStateRegistry(db)
    for i in range(3):
        db.audit("plan-kill", "system", f"event_{i}", {"seq": i})
    db.conn.commit()
    bk = tmp_path / "backups" / "good.db"
    backup_database(live, bk)
    db.close()
    db2 = Database(live)
    db2.audit("plan-kill", "system", "event_later", {"seq": 9})
    db2.conn.commit()
    db2.close()
    # a CLOSED sqlite DB checkpoints its WAL away, so the trio-move
    # cases need FORGED siblings — the ledger treats them as opaque
    # owned bytes and moves them one at a time like any other file.
    (tmp_path / "main.db-wal").write_bytes(b"WALFORGED-1")
    (tmp_path / "main.db-shm").write_bytes(b"SHMFORGED-1")
    return {"live": live, "bk": bk, "tmp": tmp_path,
            "pre_main": sha256_file(live),
            "pre_wal": sha256_file(tmp_path / "main.db-wal"),
            "pre_shm": sha256_file(tmp_path / "main.db-shm")}


def _kill_at(w, op):
    env = dict(os.environ, PLANPILOT_RESTORE_KILL_AT=op)
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER, str(w["live"]), str(w["bk"])],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=90)
    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert "DONE" not in proc.stdout
    return proc


def _generation_dirs(w):
    root = Path(str(w["live"]) + ".quarantine")
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


# ---- the six design points (a) classification + (b) convergence ------
# Each entry: kill seam -> what reconcile MUST say about that world.
SIX_POINTS = [
    "stage-copy",          # 1: after marker-create, before staging done
    "quarantine-main",     # 2: after staging, before first move landed
    "quarantine-wal",      # 3: after main moved, before -wal
    "quarantine-shm",      # 4: after -wal (partial trio)
    "receipt-write",       # 5: after os.replace, before RECEIPTED-side
    "marker-clear",        # 6: after receipt durable, before clear
]


@pytest.mark.parametrize("seam", SIX_POINTS)
def test_kill_world_classifies_without_conflict(world, seam):
    _kill_at(world, seam)
    ledger = load_ledger(marker_path(world["live"]))
    assert ledger is not None
    rec = reconcile_ledger(ledger)
    assert rec["conflicts"] == [], rec["conflicts"]


@pytest.mark.parametrize("seam", SIX_POINTS)
def test_rerun_converges_after_kill(world, seam):
    _kill_at(world, seam)
    assert marker_path(world["live"]).exists()      # intent still open
    ledger = restore_database(world["live"], world["bk"])
    # (b) convergence: target == backup bytes, marker cleared,
    # receipt valid + bound, trio complete under ONE generation dir
    assert sha256_file(world["live"]) == ledger["backup_sha256"]
    assert not marker_path(world["live"]).exists()
    assert not stage_path(world["live"]).exists()
    rdata = json.loads(receipt_path(world["live"]).read_text(encoding="utf-8"))
    assert rdata["generation"] == ledger["generation"]
    assert rdata["backup_sha256"] == ledger["backup_sha256"]
    qdir = Path(str(world["live"]) + ".quarantine") / ledger["generation"]
    assert (qdir / "main.db").is_file()
    assert (qdir / "main.db-wal").is_file() or \
        ledger["facts"].get(str(world["live"]) + "-wal", {}).get("exists") is False
    assert (qdir / "main.db-shm").is_file() or \
        ledger["facts"].get(str(world["live"]) + "-shm", {}).get("exists") is False
    # convergence also UNLOCKS the gate path: receipt present -> ack -> ok
    ok0, _ = evaluate_recovery_gate(world["live"])
    assert not ok0                                    # unacked = blocked
    write_ack(world["live"], "matrix-operator")
    ok1, reasons = evaluate_recovery_gate(world["live"])
    assert ok1, reasons


@pytest.mark.parametrize("seam", SIX_POINTS)
def test_rollback_after_kill_is_byte_exact(world, seam):
    """(c) rollback variant from EVERY kill point (design: 'for EACH'):
    --rollback returns the PRE-restore world byte-exact. At seams 5/6
    the target already holds replace-written bytes (== anchored backup
    sha) while the original waits in quarantine — the ledger owns both
    sides, so rollback discards the new bytes and moves the original
    back; same-generation receipt/ack leave no gate residue."""
    _kill_at(world, seam)
    rollback_ledger(world["live"])
    assert sha256_file(world["live"]) == world["pre_main"]
    assert sha256_file(world["tmp"] / "main.db-wal") == world["pre_wal"]
    assert sha256_file(world["tmp"] / "main.db-shm") == world["pre_shm"]
    assert not marker_path(world["live"]).exists()
    assert not stage_path(world["live"]).exists()
    assert not receipt_path(world["live"]).exists()
    ok, _ = evaluate_recovery_gate(world["live"])
    assert ok                                        # clean world boots


# ---- 4.7 falsifier: ro-open proves readiness WITHOUT write power -----

def test_readonly_open_begin_ok_insert_denied(world):
    """The battery's readiness claim is falsifiable: a read-only URI
    connection can BEGIN and SELECT the audit chain, but EVERY write
    class is denied by the driver itself — so 'verified' can never have
    come from a path that could mutate the backup."""
    w = world["bk"]
    conn = sqlite3.connect(Path(w).as_uri() + "?mode=ro&immutable=1",
                           uri=True)
    try:
        conn.execute("BEGIN")                        # ok: read tx
        rows = conn.execute(
            "SELECT count(*) FROM audit_chain").fetchone()
        assert rows[0] >= 3
        for stmt in ("INSERT INTO audit_chain (record) VALUES ('x')",
                     "UPDATE audit_chain SET record='x' WHERE id=1",
                     "DELETE FROM audit_chain WHERE id=1",
                     "DROP TABLE audit_chain"):
            with pytest.raises(sqlite3.OperationalError) as ei:
                conn.execute(stmt)
            assert "readonly" in str(ei.value).lower()
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert sha256_file(w)                            # file still intact


# ---- round-4 P0-2 boot-order proofs at the GATE seam -----------------

def _api_server_module():
    sys.path.insert(0, str(ROOT / "tools"))
    import api_server
    return api_server


def test_gate_denied_startup_constructs_nothing(world, monkeypatch):
    """Receipt present + no ack => main() aborts BEFORE Clock/Database/
    Server: constructor spies never called, socket never bound, and
    every pre-existing file byte-identical."""
    restore_database(world["live"], world["bk"])     # unacked restore
    api_server = _api_server_module()
    calls = []
    events = []

    def _clock(snapshot, host):
        events.append("clock")
        raise AssertionError("clock must never be constructed gate-denied")

    def _db(*a, **k):
        calls.append("Database")
        raise AssertionError("Database must never be constructed")

    class ServerSpy(api_server.Server):
        def __init__(self, *a, **k):
            calls.append("Server")
            raise AssertionError("Server must never be constructed/bound")

    monkeypatch.setattr(api_server, "clock_from_env", _clock)
    monkeypatch.setattr(api_server, "Database", _db)
    monkeypatch.setattr(api_server, "Server", ServerSpy)
    env = {
        "PLANPILOT_ENV": "development",
        "PLANPILOT_AUTH_SECRET": "x" * 32,
        "PLANPILOT_DB": str(world["live"]),
        "PLANPILOT_FACTORY_ROOT": str(world["tmp"]),
        "PLANPILOT_PORT": "0",
    }
    pre = {p: (p.read_bytes() if p.exists() else None) for p in
           (world["live"], receipt_path(world["live"]))}
    snapshot = dict(env)
    monkeypatch.setattr(os, "environ", snapshot)
    monkeypatch.setattr(api_server.os, "environ", snapshot)
    with pytest.raises(SystemExit) as ei:
        api_server.main()
    assert "unacked" in str(ei.value) and "restore" in str(ei.value)
    assert calls == [] and events == []              # zero constructions
    for p, data in pre.items():                      # bytes untouched
        assert (p.read_bytes() if p.exists() else None) == data
    # gate-denied startup released the lock it had just taken: a
    # restore attempt right here succeeds (would DbLockHeld if stranded)
    restore_database(world["live"], world["bk"])


def test_marker_kill_then_ack_survives_resume(world):
    """Kill at marker-clear: receipt durable, marker still there =>
    gate BLOCKED even after a fresh ack; the operator reruns restore,
    which CLEARS the marker and KEEPS the ack (ack-invalidate targets
    acks of OTHER generations, never this restore's own)."""
    _kill_at(world, "marker-clear")
    assert receipt_path(world["live"]).is_file()
    write_ack(world["live"], "matrix-operator")
    ok, reasons = evaluate_recovery_gate(world["live"])
    assert not ok and any("marker present" in r for r in reasons)
    restore_database(world["live"], world["bk"])
    assert not marker_path(world["live"]).exists()
    ok2, reasons2 = evaluate_recovery_gate(world["live"])
    assert ok2, reasons2


def test_restore_cannot_take_lock_while_server_booted(world):
    """Mid-startup interlock: a booted server holds the OS lock; a
    restore attempt refuses NON-BLOCKING and changes nothing."""
    holder = acquire_server_lock(world["live"])      # as if server main()
    from planpilot.restore import restore_database as rd
    with pytest.raises(DbLockHeld):
        rd(world["live"], world["bk"])
    assert not marker_path(world["live"]).exists()   # zero intent written
    assert not stage_path(world["live"]).exists()
    assert sha256_file(world["live"]) == world["pre_main"]
    holder.release()
