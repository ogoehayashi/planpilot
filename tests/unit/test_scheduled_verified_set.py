"""G4 4.3 — scheduled backup enters the verified set ONLY through V1.

Design §5 V3: failure quarantines the file to <dir>/unverified/, never
counted in retention, never advertised; success writes the atomic
last_verified_backup.json manifest that deep-health reads for age.
Corrupt-copy regression + retention + manifest schema pinned here.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

import pytest

from planpilot.audit import AuditTrail
from planpilot.backup import sha256_file, verify_backup_file
from planpilot.factory_state import FactoryStateRegistry
from planpilot.persistence import Database
from tools.scheduled_backup import MANIFEST_NAME, run


def _full_source(tmp_path: Path) -> Path:
    db = Database(tmp_path / "main.db")
    AuditTrail(db)
    FactoryStateRegistry(db)
    for i in range(2):
        db.audit("plan-sched", "system", f"event_{i}", {"seq": i})
    db.conn.commit()
    db.close()
    return tmp_path / "main.db"


def _bare_source(tmp_path: Path) -> Path:
    """A Database that never ran component init: battery-refusable on
    required_tables while the sqlite copy itself is structurally fine."""
    db = Database(tmp_path / "bare.db")
    db.conn.commit()
    db.close()
    return tmp_path / "bare.db"


def test_full_source_backup_is_verified_and_advertised(tmp_path):
    src = _full_source(tmp_path)
    out = tmp_path / "backups"
    result = run(src, out, keep=3)
    assert result["backup"] is not None
    backup = Path(result["backup"])
    assert backup.is_file() and not (out / "unverified").exists()
    manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["path"] == str(backup)
    assert manifest["sha256"] == sha256_file(backup)
    assert manifest["chain_head"]["entry_count"] == 2
    tip = verify_backup_file(backup)
    assert tip.ok and tip.event_hash == manifest["chain_head"]["event_hash"]
    # stamp is RFC3339-with-Z readable by the deep-health parser rule
    epoch = datetime.datetime.fromisoformat(
        manifest["verified_at"].replace("Z", "+00:00")).timestamp()
    assert epoch <= datetime.datetime.now(datetime.timezone.utc).timestamp()


def test_refused_backup_is_quarantined_never_advertised(tmp_path):
    """V3 core: a created-but-refused file moves to unverified/, the
    retention set never sees it, and the manifest stays untouched."""
    full = _full_source(tmp_path)
    run(full, tmp_path / "b", keep=3)          # one good cycle first
    manifest_before = (tmp_path / "b" / MANIFEST_NAME).read_bytes()
    bad = _bare_source(tmp_path)               # passes copy, fails battery
    result = run(bad, tmp_path / "b", keep=3)
    assert result["backup"] is None
    quarantined = Path(result["quarantined"])
    assert quarantined.is_file()
    assert "required_tables" in result["failure"]
    assert (tmp_path / "b" / MANIFEST_NAME).read_bytes() == manifest_before
    # quarantine file is NOT counted by the retention name scan
    counted = [p.name for p in (tmp_path / "b").iterdir()
               if p.name.startswith("planpilot-") and p.suffix == ".db"]
    assert quarantined.name not in counted


def test_retention_bounds_the_verified_set_only(tmp_path):
    out = tmp_path / "b"
    src = _full_source(tmp_path)
    names = []
    for _ in range(3):
        names.append(Path(run(src, out, keep=2)["backup"]).name)
    remaining = sorted(p.name for p in out.iterdir()
                      if p.name.startswith("planpilot-") and p.suffix == ".db")
    assert remaining == sorted(names[1:])      # newest two kept
    assert names[0] not in remaining


def test_keep_below_one_rejects(tmp_path):
    src = _full_source(tmp_path)
    with pytest.raises(ValueError):
        run(src, tmp_path / "b", keep=0)


def test_quarantine_dir_is_bounded_on_success(tmp_path):
    """Failed files must not accumulate unboundedly: the success path
    prunes unverified/ with the same keep (V3 retention bound)."""
    out = tmp_path / "b"
    full = _full_source(tmp_path)
    bare = _bare_source(tmp_path)
    run(full, out, keep=2)
    for _ in range(3):
        assert run(bare, out, keep=2)["backup"] is None
    qdir = out / "unverified"
    assert len(list(qdir.iterdir())) == 3
    good = run(full, out, keep=2)      # success prunes quarantine too
    assert any(r.startswith("unverified/") for r in good["removed"])
    assert len(list(qdir.iterdir())) == 2   # newest two survive
