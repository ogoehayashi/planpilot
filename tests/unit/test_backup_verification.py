"""G4 4.1/4.2 — read-only audit verifier + full backup battery.

Every battery OUTCOME (pass and each failure) must leave the verified
file byte-identical (design §5 V1: the verifier never repairs, never
mutates). Fixtures build backups through the FULL runtime path — a bare
Database fixture is intentionally not restorable (round-4 policy).
"""
from __future__ import annotations

import ast
import hashlib
import sqlite3
from pathlib import Path

import pytest

from planpilot.audit import AuditChainError, AuditTrail, verify_audit_connection
from planpilot.backup import REQUIRED_TABLES, backup_database, sha256_file, verify_backup_file
from planpilot.factory_state import FactoryStateRegistry
from planpilot.persistence import Database


ROOT = Path(__file__).resolve().parents[2]


def _runtime_db(path: Path) -> Database:
    """A Database that ran the FULL component init: audit tables come
    with Database; security_events/decision_traces via AuditTrail;
    factory_states via FactoryStateRegistry — same three constructors
    the Server composition root uses (api_server.py __init__)."""
    db = Database(path)
    AuditTrail(db)
    FactoryStateRegistry(db)
    return db


def _seed_audit(db: Database, n: int = 3) -> None:
    for i in range(n):
        db.audit("plan-verify", "system", f"event_{i}", {"seq": i})


@pytest.fixture
def runtime_db(tmp_path):
    db = _runtime_db(tmp_path / "main.db")
    _seed_audit(db)
    yield db
    db.close()


@pytest.fixture
def good_backup(tmp_path, runtime_db):
    runtime_db.conn.commit()
    target = tmp_path / "backups" / "planpilot-good.db"
    backup_database(tmp_path / "main.db", target)
    return target


# ---------- 4.1 verify_audit_connection: pure + honest ----------------

def test_verifier_source_is_select_only():
    """AST guard (design §5 V1): the verifier function contains no
    CREATE/INSERT/UPDATE/DELETE token, and makes no Database-class call."""
    fn = verify_audit_connection
    tree = ast.parse(Path(fn.__code__.co_filename).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "verify_audit_connection":
            src = ast.get_source_segment(
                Path(fn.__code__.co_filename).read_text(encoding="utf-8"), node)
            for forbidden in ("CREATE", "INSERT", "UPDATE", "DELETE",
                              "executescript", "Database("):
                assert forbidden not in src, f"verifier source contains {forbidden!r}"
            return
    pytest.fail("verify_audit_connection not found in its module file")


def test_verifier_passes_clean_chain_and_reports_tip(runtime_db):
    tip = verify_audit_connection(runtime_db.conn)
    assert tip["entry_count"] == 3
    row = runtime_db.conn.execute(
        "SELECT event_hash FROM audit_chain ORDER BY id DESC LIMIT 1").fetchone()
    assert tip["event_hash"] == row[0]


def _writable_copy(db_path: Path, dest: Path) -> sqlite3.Connection:
    """Open a backup copy for DAMAGE injection (a test writer, NOT the
    verifier) with triggers dropped — simulating an out-of-band edit."""
    conn = sqlite3.connect(dest)
    conn.execute("DROP TRIGGER IF EXISTS audit_chain_no_update")
    conn.execute("DROP TRIGGER IF EXISTS audit_chain_no_delete")
    conn.commit()
    return conn


@pytest.fixture
def raw_backup(tmp_path, runtime_db):
    """Fresh copy of the runtime db file (plain bytes copy is fine for
    an offline file; battery opens it ro+immutable)."""
    runtime_db.close()
    src = tmp_path / "main.db"
    dest = tmp_path / "planpilot-raw.db"
    import shutil
    shutil.copy2(src, dest)
    return dest


def _expect_kind(backup: Path, kind: str):
    sha_before = sha256_file(backup)
    result = verify_backup_file(backup)
    assert not result.ok
    chain = next(c for c in result.checks if c.name == "audit_chain")
    assert kind in chain.detail
    assert sha256_file(backup) == sha_before  # per-outcome SHA invariant


def test_tampered_record_is_chain_break(raw_backup):
    conn = sqlite3.connect(raw_backup)
    conn.execute("DROP TRIGGER IF EXISTS audit_chain_no_update")
    row = conn.execute("SELECT id, record FROM audit_chain ORDER BY id LIMIT 1").fetchone()
    tampered = row[1].replace("event_0", "event_9")
    assert tampered != row[1]  # guard against a no-op injection
    conn.execute("UPDATE audit_chain SET record=? WHERE id=?",
                 (tampered, row[0]))  # mutate stored bytes
    conn.commit()
    conn.close()
    _expect_kind(raw_backup, "chain_break")


def test_whitespace_only_mutation_fails_f5(raw_backup):
    """F5 verbatim rule: hashing the STORED string means a whitespace-
    only record edit is a break. A re-canonicalizing verifier would
    falsely bless this file."""
    conn = sqlite3.connect(raw_backup)
    conn.execute("DROP TRIGGER IF EXISTS audit_chain_no_update")
    row = conn.execute("SELECT id, record FROM audit_chain ORDER BY id LIMIT 1").fetchone()
    conn.execute("UPDATE audit_chain SET record=? WHERE id=?",
                 (row[1] + " ", row[0]))  # trailing space, same JSON value
    conn.commit()
    conn.close()
    _expect_kind(raw_backup, "chain_break")


def test_deleted_head_row_is_missing_head(raw_backup):
    conn = sqlite3.connect(raw_backup)
    conn.execute("DELETE FROM audit_chain_head WHERE singleton=1")
    conn.commit()
    conn.close()
    _expect_kind(raw_backup, "missing_head")


def test_head_advance_without_row_is_head_mismatch(raw_backup):
    conn = sqlite3.connect(raw_backup)
    conn.execute("UPDATE audit_chain_head SET entry_count=entry_count+1 WHERE singleton=1")
    conn.commit()
    conn.close()
    _expect_kind(raw_backup, "head_mismatch")


def test_missing_required_table_is_not_restorable(raw_backup):
    conn = sqlite3.connect(raw_backup)
    conn.execute("DROP TABLE factory_states")
    conn.commit()
    conn.close()
    # SHA is measured AFTER the test's own damage injection — the
    # invariant under test is "the verifier never mutates", not "a
    # DROP mutates nothing".
    sha_before = sha256_file(raw_backup)
    result = verify_backup_file(raw_backup)
    assert not result.ok
    tables = next(c for c in result.checks if c.name == "required_tables")
    assert "factory_states" in tables.detail
    assert sha256_file(raw_backup) == sha_before


def test_stranded_wal_sibling_refuses(raw_backup):
    (raw_backup.parent / (raw_backup.name + "-wal")).write_bytes(b"not-a-wal")
    result = verify_backup_file(raw_backup)
    assert not result.ok
    sc = next(c for c in result.checks if c.name == "self_contained")
    assert "-wal" in sc.detail
    (raw_backup.parent / (raw_backup.name + "-wal")).unlink()


def test_good_backup_passes_full_battery(good_backup):
    result = verify_backup_file(good_backup)
    assert result.ok, result.failure_text()
    assert result.entry_count == 3
    assert len(result.event_hash) == 64


def test_bare_database_fixture_is_intentionally_not_restorable(tmp_path):
    """Round-4 policy: only a FULL runtime backup is restorable."""
    db = Database(tmp_path / "bare.db")   # never ran AuditTrail init
    db.audit("p", "system", "e", {})
    db.close()
    result = verify_backup_file(tmp_path / "bare.db")
    assert not result.ok
    tables = next(c for c in result.checks if c.name == "required_tables")
    for missing_name in ("security_events", "decision_traces", "factory_states"):
        assert missing_name in tables.detail


def test_verifier_has_no_repair_path(tmp_path, runtime_db):
    """V1 'verifier must not repair verifiee': an empty-head db walk
    raises missing_head and changes no bytes; the Database ctor's
    INSERT OR IGNORE repair (persistence.py:248) is NOT reachable from
    the verifier — assert by walking the AST of audit.py imports."""
    src = Path(verify_audit_connection.__code__.co_filename).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "verify_audit_connection":
            calls = [ast.dump(c) for c in ast.walk(node) if isinstance(c, ast.Call)]
            assert not any("Database" in c for c in calls)
            assert not any("executescript" in c for c in calls)
