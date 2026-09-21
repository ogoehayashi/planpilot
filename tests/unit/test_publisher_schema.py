"""G2 task 1.1 — pin the publication_receipt / idempotency_registry schema.

Design §3: two tables written only inside Database.lock/transaction(),
no triggers, no audit columns, explicit UNIQUE sets, and the one FK —
foreign_keys is ON (the pragma Database sets on every connect), and a
reopened Database keeps the rows.
"""
import sqlite3

import pytest

from planpilot.persistence import Database


RECEIPT_ROW = {
    "plan_id": "plan_x1",
    "plan_version": 1,
    # Contract-legal shapes throughout (reviewer round-4 probe 1):
    # plan_digest matches ^[a-f0-9]{64}$, fingerprints are the bare
    # sha256 hexdigests request_fingerprint() actually stores.
    "plan_digest": "a" * 64,
    "approval_set_id": "as_1",
    "audit_log_id": "log_1",
    "response_json": '{"status":"PUBLISHED","plan_id":"plan_x1",'
                     '"published_version":1,"audit_log_id":"log_1"}',
    "created_at": "2026-09-14T09:00:00+08:00",
}

REGISTRY_ROW = {
    "tool_name": "publish_plan",
    "idempotency_key": "key-1",
    "request_fingerprint": "b" * 64,
    "receipt_id": 1,
    "created_at": "2026-09-14T09:00:00+08:00",
}


def _insert(conn, table, row):
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                 list(row.values()))


def _table_columns(conn, table):
    return {row[1]: (row[2], row[5]) for row in
            conn.execute(f"PRAGMA table_info({table})")}


def test_tables_exist_with_declared_columns(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    conn = db.conn
    assert _table_columns(conn, "publication_receipt") == {
        "id": ("INTEGER", 1),
        "plan_id": ("TEXT", 0),
        "plan_version": ("INTEGER", 0),
        "plan_digest": ("TEXT", 0),
        "approval_set_id": ("TEXT", 0),
        "audit_log_id": ("TEXT", 0),
        "response_json": ("TEXT", 0),
        "created_at": ("TEXT", 0),
    }
    assert _table_columns(conn, "idempotency_registry") == {
        "id": ("INTEGER", 1),
        "tool_name": ("TEXT", 0),
        "idempotency_key": ("TEXT", 0),
        "request_fingerprint": ("TEXT", 0),
        "receipt_id": ("INTEGER", 0),
        "created_at": ("TEXT", 0),
    }
    # FK declared exactly once, referencing publication_receipt(id).
    fks = conn.execute("PRAGMA foreign_key_list(idempotency_registry)").fetchall()
    assert [(tuple(f)[2], tuple(f)[3], tuple(f)[4]) for f in fks] == \
        [("publication_receipt", "receipt_id", "id")]
    db.close()


def test_receipt_unique_plan_version_and_audit_log(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    with db.transaction():
        _insert(db.conn, "publication_receipt", RECEIPT_ROW)
    # same (plan_id, plan_version) twice -> UNIQUE violation
    with pytest.raises(sqlite3.IntegrityError), db.transaction():
        _insert(db.conn, "publication_receipt",
                {**RECEIPT_ROW, "audit_log_id": "log_2"})
    # same audit_log_id under a different version -> UNIQUE violation
    with pytest.raises(sqlite3.IntegrityError), db.transaction():
        _insert(db.conn, "publication_receipt",
                {**RECEIPT_ROW, "plan_version": 2})
    db.close()


def test_registry_unique_key_per_tool(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    with db.transaction():
        _insert(db.conn, "publication_receipt", RECEIPT_ROW)
        _insert(db.conn, "idempotency_registry", REGISTRY_ROW)
    # same (tool_name, key) twice -> UNIQUE violation even with a new
    # fingerprint (Case B is a constraint, not application logic)
    with pytest.raises(sqlite3.IntegrityError), db.transaction():
        _insert(db.conn, "idempotency_registry",
                {**REGISTRY_ROW, "request_fingerprint": "c" * 64})
    # same key under a different tool IS allowed (scope is per tool)
    with db.transaction():
        _insert(db.conn, "idempotency_registry",
                {**REGISTRY_ROW, "tool_name": "other_tool"})
    db.close()


def test_foreign_key_enforced_on_registry_receipt(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError), db.transaction():
        _insert(db.conn, "idempotency_registry",
                {**REGISTRY_ROW, "receipt_id": 999})
    db.close()


def test_tables_survive_reopen(tmp_path):
    path = str(tmp_path / "t.db")
    db = Database(path)
    with db.transaction():
        _insert(db.conn, "publication_receipt", RECEIPT_ROW)
        _insert(db.conn, "idempotency_registry", REGISTRY_ROW)
    db.close()
    db2 = Database(path)
    rows = [tuple(r) for r in db2.conn.execute(
        "SELECT plan_id, plan_version, audit_log_id FROM publication_receipt")]
    assert rows == [("plan_x1", 1, "log_1")]
    assert db2.conn.execute(
        "SELECT request_fingerprint FROM idempotency_registry").fetchone()[0] \
        == "b" * 64
    db2.close()
