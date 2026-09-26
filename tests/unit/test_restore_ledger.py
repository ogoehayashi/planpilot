"""G4 4.5/4.6 — intent-ledger restore + recovery gate + ack semantics.

Fixtures build backups through the FULL runtime path (a bare Database
backup is intentionally not restorable, round-4 policy). Every refusal
must name WHY; every convergence claim is pinned on byte-level SHA
equality, never on a phase label.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from planpilot.audit import AuditTrail
from planpilot.backup import backup_database, sha256_file
from planpilot.factory_state import FactoryStateRegistry
from planpilot.persistence import Database
from planpilot.restore import (
    RestoreAborted, ack_path, current_phase, evaluate_recovery_gate,
    load_ledger, marker_path, receipt_path, reconcile_ledger,
    restore_database, rollback_ledger, stage_path, write_ack,
)
from planpilot.db_lock import acquire_server_lock, DbLockHeld


def _runtime_db(path: Path) -> Database:
    db = Database(path)
    AuditTrail(db)
    FactoryStateRegistry(db)
    for i in range(3):
        db.audit("plan-restore", "system", f"event_{i}", {"seq": i})
    db.conn.commit()
    return db


@pytest.fixture
def world(tmp_path):
    """live DB + a verified backup taken from it, then live moves on."""
    live = tmp_path / "main.db"
    db = _runtime_db(live)
    bk = tmp_path / "backups" / "good.db"
    backup_database(live, bk)
    db.close()
    # post-backup writes: the restore has real bytes to revert
    db2 = _runtime_db(live)
    db2.audit("plan-restore", "system", "event_later", {"seq": 99})
    db2.conn.commit()
    db2.close()
    return {"tmp": tmp_path, "live": live, "backup": bk,
            "pre_restore_sha": sha256_file(live)}


# ---------------- 4.5 happy path + facts, never labels ----------------

def test_clean_restore_converges(world):
    ledger = restore_database(world["live"], world["backup"])
    assert sha256_file(world["live"]) == ledger["backup_sha256"]
    assert not marker_path(world["live"]).exists()
    assert ledger["ops"]["marker-clear"] == "done"
    qdir = world["tmp"] / "main.db.quarantine" / ledger["generation"]
    assert (qdir / "main.db").is_file()          # original, original name
    assert current_phase(ledger) == "COMPLETE"


def test_receipt_binds_every_required_field(world):
    ledger = restore_database(world["live"], world["backup"])
    data = json.loads(receipt_path(world["live"]).read_text(encoding="utf-8"))
    assert data["backup_sha256"] == ledger["backup_sha256"]
    assert data["backup_path"] == str(world["backup"])
    assert data["target_db"] == str(world["live"])
    assert data["audit_head_entry_count"] == ledger["verified_tip"]["entry_count"]
    assert data["event_hash"] == ledger["verified_tip"]["event_hash"]
    assert data["generation"] == ledger["generation"]
    assert data["quarantine_dir"].endswith(".quarantine")
    assert data["restored_at"] and data["tool_version"]


def test_bare_database_backup_is_not_restorable(world):
    bare = world["tmp"] / "bare.db"
    Database(bare).close()                       # no audit/factory tables
    out = world["tmp"] / "bare-target.db"
    out.write_bytes(b"old bytes")
    with pytest.raises(RestoreAborted, match="never restorable"):
        restore_database(out, bare)
    # refusal happens BEFORE any marker exists: zero intent written
    assert not marker_path(out).exists()


def test_reconcile_derives_from_bytes_not_labels(world):
    """Kill-#5 world: replace already happened (target == backup bytes)
    but ops/phase fields still read QUARANTINED. Facts must outvote the
    label: everything up to the replace counts done, no conflicts."""
    from planpilot.restore import new_ledger, save_ledger, _new_generation, \
        _touch_ledger_facts
    live, bk = world["live"], world["backup"]
    ledger = new_ledger()
    ledger["target_db"] = str(live)
    ledger["backup_path"] = str(bk)
    from planpilot.backup import verify_backup_file
    ledger["backup_sha256"] = verify_backup_file(bk).sha256
    ledger["generation"] = _new_generation(live)
    _touch_ledger_facts(ledger)
    # simulate the world after a hard kill right after os.replace:
    qdir = live.parent / "main.db.quarantine" / ledger["generation"]
    import shutil as _sh
    _sh.move(str(live), str(qdir / live.name))
    _sh.copyfile(bk, live)
    for suffix in ("-wal", "-shm"):
        if (live.parent / (live.name + suffix)).exists():
            _sh.move(str(live.parent / (live.name + suffix)),
                     str(qdir / (live.name + suffix)))
    save_ledger(marker_path(live), ledger)       # ops still ALL pending
    rec = reconcile_ledger(ledger)
    assert rec["conflicts"] == []
    assert rec["ops"]["replace-staging"] == "done"
    assert rec["ops"]["quarantine-main"] == "done"
    # and the marker-on-disk LABEL was 'pending' — facts outvoted it
    assert json.loads(marker_path(live).read_text())["ops"][
        "replace-staging"] == "pending"


def test_second_restore_of_same_backup_gets_fresh_generation(world):
    l1 = restore_database(world["live"], world["backup"])
    l2 = restore_database(world["live"], world["backup"])
    assert l2["generation"] != l1["generation"]
    assert sha256_file(world["live"]) == l2["backup_sha256"]


# ---------------- 4.6 gate + ack: the P0-1 regressions ----------------

def test_unacked_restore_blocks_the_gate(world):
    restore_database(world["live"], world["backup"])
    ok, reasons = evaluate_recovery_gate(world["live"])
    assert not ok and any("no ack file" in r for r in reasons)


def test_second_restore_of_same_backup_voids_old_ack(world):
    """P0-1 #1: acks are PER RESTORE. Restore -> ack -> restore the SAME
    backup again: the ledger's ack-invalidate op deletes the old ack
    BEFORE touching target files, so the gate blocks the second restore
    until its OWN ack exists."""
    restore_database(world["live"], world["backup"])
    write_ack(world["live"], "operator-1")
    ok, _ = evaluate_recovery_gate(world["live"])
    assert ok
    restore_database(world["live"], world["backup"])
    assert not ack_path(world["live"]).exists()   # invalidated by ledger
    ok2, reasons2 = evaluate_recovery_gate(world["live"])
    assert not ok2 and any("no ack file" in r for r in reasons2)
    write_ack(world["live"], "operator-2")        # its own ack re-opens it
    ok3, reasons3 = evaluate_recovery_gate(world["live"])
    assert ok3, reasons3


def test_acked_then_written_db_boots_fine(world):
    """P0-1 #2: restored_db_sha256_at_ack is ack-time evidence ONLY.
    After the ack the live DB legitimately takes writes; the gate must
    NOT re-compare that hash at check time."""
    restore_database(world["live"], world["backup"])
    write_ack(world["live"], "operator-1")
    db = _runtime_db(world["live"])              # post-ack writes
    db.audit("plan-restore", "system", "event_new", {"seq": 7})
    db.conn.commit()
    db.close()
    ok, reasons = evaluate_recovery_gate(world["live"])
    assert ok, reasons


def test_receipt_byte_edit_voids_ack(world):
    """P0-1 #3: ack binds the receipt FILE's own hash. Edit one byte
    while keeping the JSON valid (a changed field VALUE): the gate must
    reject on receipt_sha256, not on parseability — this proves the hash
    binding does its job even for well-formed forgeries."""
    restore_database(world["live"], world["backup"])
    write_ack(world["live"], "operator-1")
    r = receipt_path(world["live"])
    data = json.loads(r.read_text(encoding="utf-8"))
    ts = data["restored_at"]
    data["restored_at"] = ts[:-1] + ("0" if ts[-1] != "0" else "1")
    r.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    ok, reasons = evaluate_recovery_gate(world["live"])
    assert not ok
    assert any("receipt_sha256" in x for x in reasons)


@pytest.mark.parametrize("field", [
    "receipt_sha256", "generation", "backup_sha256", "target_db"])
def test_each_security_field_edit_rejects(world, field):
    """P0-1 #4: EVERY mismatch on the four security-binding fields
    rejects — edited in isolation, the other three still correct."""
    restore_database(world["live"], world["backup"])
    write_ack(world["live"], "operator-1")
    ack = ack_path(world["live"])
    data = json.loads(ack.read_text(encoding="utf-8"))
    data[field] = "a" * 64 if field.endswith("sha256") else "tampered-x"
    ack.write_text(json.dumps(data), encoding="utf-8")
    ok, reasons = evaluate_recovery_gate(world["live"])
    assert not ok and any(field in r for r in reasons)


def test_malformed_or_missing_ack_rejects(world):
    restore_database(world["live"], world["backup"])
    ack = ack_path(world["live"])
    ack.write_text("{not json", encoding="utf-8")
    ok, reasons = evaluate_recovery_gate(world["live"])
    assert not ok and any("malformed" in r for r in reasons)
    ack.unlink()
    ok2, reasons2 = evaluate_recovery_gate(world["live"])
    assert not ok2 and any("no ack file" in r for r in reasons2)


def test_no_restore_at_all_passes_gate(world):
    ok, reasons = evaluate_recovery_gate(world["live"])
    assert ok and reasons == []


def test_operator_timestamp_are_provenance_not_security(world):
    """Round-5 boundary, claimed EXACTLY: same-format edits to
    operator/acked_at are NOT claimed detectable. The gate does not
    check them — this test pins the design boundary, it is not a
    detection guarantee."""
    restore_database(world["live"], world["backup"])
    write_ack(world["live"], "operator-1")
    ack = ack_path(world["live"])
    data = json.loads(ack.read_text(encoding="utf-8"))
    data["operator"] = "someone-else"
    data["acked_at"] = "20200101T000000.000000Z"
    ack.write_text(json.dumps(data), encoding="utf-8")
    ok, _ = evaluate_recovery_gate(world["live"])
    assert ok                             # by design: provenance only


# ---------------- 4.5 rollback: byte-exact or refuse ----------------

def test_rollback_returns_pre_restore_world_byte_exact(world):
    """A genuinely mid-flight ledger (killed right after quarantine-main
    moved): rollback puts the ORIGINAL bytes back byte-exact, removes
    staging + marker + the emptied generation dir."""
    import shutil
    from planpilot.backup import verify_backup_file
    from planpilot.restore import (new_ledger, save_ledger, _new_generation,
                                   _touch_ledger_facts)
    live = world["live"]
    pre = sha256_file(live)                       # post-backup mutation
    ledger = new_ledger()
    ledger["target_db"] = str(live)
    ledger["backup_path"] = str(world["backup"])
    ledger["backup_sha256"] = verify_backup_file(world["backup"]).sha256
    ledger["generation"] = _new_generation(live)
    _touch_ledger_facts(ledger)                   # pre-move facts recorded
    save_ledger(marker_path(live), ledger)
    qdir = live.parent / "main.db.quarantine" / ledger["generation"]
    shutil.move(str(live), str(qdir / live.name))  # as if killed here
    shutil.copyfile(world["backup"], stage_path(live))
    rollback_ledger(live)
    assert sha256_file(live) == pre               # byte-exact original
    assert not stage_path(live).exists()
    assert not marker_path(live).exists()
    assert not qdir.exists()                       # emptied dir removed


def test_rollback_refuses_to_clobber_unknown_bytes(world):
    """Target slot holding bytes the ledger never quarantined and never
    replaced => refusal, files untouched."""
    from planpilot.backup import verify_backup_file
    from planpilot.restore import (new_ledger, save_ledger, _new_generation,
                                   _touch_ledger_facts)
    live = world["live"]
    pre = sha256_file(live)
    ledger = new_ledger()
    ledger["target_db"] = str(live)
    ledger["backup_path"] = str(world["backup"])
    ledger["backup_sha256"] = verify_backup_file(world["backup"]).sha256
    ledger["generation"] = _new_generation(live)
    _touch_ledger_facts(ledger)
    save_ledger(marker_path(live), ledger)
    qdir = live.parent / "main.db.quarantine" / ledger["generation"]
    # quarantine NEVER ran (dir empty) yet the target slot holds bytes
    # matching neither the recorded pre-move fact nor the anchored
    # backup: a third party wrote here — rollback must refuse.
    live.write_bytes(b"third-party-junk")
    with pytest.raises(RestoreAborted, match="unknown bytes"):
        rollback_ledger(live)
    assert live.read_bytes() == b"third-party-junk"  # zero mutation
    assert marker_path(live).exists()


def test_rollback_without_marker_refuses(world):
    with pytest.raises(RestoreAborted, match="nothing to roll back"):
        rollback_ledger(world["live"])


# ---------------- 4.6 CLI: offline-only, zero writes ----------------

OFFLINE_FORBIDDEN = ("Server(", "Database(", "RuntimeAuthority(",
                    "ScenarioClock(", "api_server", "import sqlite3")


def test_cli_source_constructs_no_server_or_database():
    """Design §5 R4 / tasks 4.6: the offline CLIs build NO Server,
    Database, RuntimeAuthority or ScenarioClock — checked in CONSTRUCT
    form (call syntax / imports), not prose. The module docstring is
    allowed to NAME the forbidden things precisely because it explains
    why they are absent."""
    src = (Path(__file__).resolve().parents[2] / "tools" /
           "restore_database.py").read_text(encoding="utf-8")
    code = src.split('"""')[2]                  # everything after the docstring
    for banned in OFFLINE_FORBIDDEN:
        assert banned not in code, f"offline CLI code references {banned!r}"


def test_cli_status_and_show_receipt_are_byte_frozen(world):
    """--status / --show-receipt write NOTHING: every sidecar byte
    identical before and after."""
    restore_database(world["live"], world["backup"])
    write_ack(world["live"], "operator-1")
    sidecars = [marker_path(world["live"]), receipt_path(world["live"]),
                ack_path(world["live"])]
    before = {p: (p.read_bytes() if p.exists() else None) for p in sidecars}
    target_sha = sha256_file(world["live"])
    cli = Path(__file__).resolve().parents[2] / "tools" / "restore_database.py"
    for args, want in ((["--status"], 0), (["--show-receipt"], 0)):
        out = subprocess.run(
            [sys.executable, str(cli), "--db", str(world["live"])] + args,
            capture_output=True, text=True, timeout=60)
        assert out.returncode == want, out.stdout + out.stderr
    for p, data in before.items():
        assert (p.read_bytes() if p.exists() else None) == data
    assert sha256_file(world["live"]) == target_sha
