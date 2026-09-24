"""G4 Phase 1 (design §2): clock_session defence in depth.

A1  _persist fails CLOSED when the anchor row is gone (no silent
    un-persisted stamp).
A2  three triggers on the REAL four columns abort delete / anchor-rewrites
    / time-rewind, while the legitimate forward high-water UPDATE stays
    allowed (regression pin for _persist).
A2' shape guard: Database.__init__ compares the STORED sqlite_master body
    of each trigger against the canonical text — a same-named hollow
    pre-plant (CREATE TRIGGER IF NOT EXISTS silently no-ops) refuses to
    open.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from planpilot.clock import ScenarioClock
from planpilot.persistence import Database, _sql_fingerprint

ANCHOR = "2026-09-14T08:00:00+08:00"


def _clock_db(tmp_path):
    db = Database(tmp_path / "k.db", clock=ScenarioClock(ANCHOR))
    return db


def _row(db):
    return db.conn.execute(
        "SELECT scenario_anchor, real_wall_started_at, last_issued_scenario_time"
        " FROM clock_session WHERE singleton=1").fetchone()


# ---------------- A2: trigger matrix on the live database ----------------

def test_delete_of_anchor_row_is_aborted(tmp_path):
    db = _clock_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="delete is forbidden"):
        db.conn.execute("DELETE FROM clock_session WHERE singleton=1")
    assert _row(db) is not None, "aborted statement must leave the row"


def test_anchor_and_wall_columns_are_immutable(tmp_path):
    db = _clock_db(tmp_path)
    for col in ("scenario_anchor", "real_wall_started_at"):
        with pytest.raises(sqlite3.IntegrityError,
                           match="anchors are immutable"):
            db.conn.execute(
                f"UPDATE clock_session SET {col}='1999-01-01T00:00:00+08:00'"
                " WHERE singleton=1")


def test_time_rewind_is_aborted_and_forward_update_passes(tmp_path):
    db = _clock_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="may not rewind"):
        db.conn.execute(
            "UPDATE clock_session SET last_issued_scenario_time=?"
            " WHERE singleton=1", ("1999-01-01T00:00:00+08:00",))
    # Regression pin: the legit high-water direction (what _persist uses)
    # MUST keep working or every clock tick would abort.
    later = "2099-01-01T00:00:00+08:00"
    db.conn.execute(
        "UPDATE clock_session SET last_issued_scenario_time=? WHERE singleton=1",
        (later,))
    assert _row(db)["last_issued_scenario_time"] == later


def test_blanket_update_where_1_cannot_reanchor(tmp_path):
    # The reviewer's `UPDATE ... WHERE 1` shape: touches the guarded
    # columns, so trigger 2 aborts the whole statement.
    db = _clock_db(tmp_path)
    before = dict(_row(db))
    with pytest.raises(sqlite3.IntegrityError, match="anchors are immutable"):
        db.conn.execute(
            "UPDATE clock_session SET scenario_anchor='X',"
            " last_issued_scenario_time='Y' WHERE 1")
    assert dict(_row(db)) == before


# ---------------- A1: _persist fails closed on a missing row -------------

def test_out_of_band_dropped_defence_self_heals_on_reopen(tmp_path):
    # Design §2 residual-hole pin (documented, NOT pretends-to-reject):
    # an attacker with raw file access can DROP TRIGGER + delete the row
    # out-of-band; our reopen cannot tell that from a pre-G4 resume file.
    # The honest behaviour: reopen REINSTALLS the canonical defence
    # (IF NOT EXISTS now creates it, digest passes) and attach re-INSERTs
    # the singleton — after that reopen, in-band deletion is blocked again.
    p = tmp_path / "k.db"
    db = Database(p, clock=ScenarioClock(ANCHOR))
    db.close()
    raw = sqlite3.connect(str(p))
    raw.execute("DROP TRIGGER clock_session_no_delete")
    raw.execute("DELETE FROM clock_session")
    raw.commit()
    raw.close()
    db2 = Database(p, clock=ScenarioClock(ANCHOR))   # opens: self-heal
    body = db2.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='clock_session_no_delete'"
    ).fetchone()[0]
    assert "RAISE" in body, "canonical defence must be reinstalled"
    assert _row(db2) is not None, "attach must recreate the singleton"
    with pytest.raises(sqlite3.IntegrityError, match="delete is forbidden"):
        db2.conn.execute("DELETE FROM clock_session WHERE singleton=1")
    db2.close()


def test_persist_row_gone_via_direct_clock_injection(tmp_path):
    # A1's own branch (row is None => RuntimeError) reached honestly:
    # construct a clock bound to a db, then delete the row via a hole the
    # shape guard cannot see (sqlite WITHOUT ROWID carve is out of scope)
    # — instead bind the clock to a fresh in-memory db whose row we drop
    # BEFORE the trigger exists by using the raw connection clock._db
    # points at. Trigger aborts DELETE, so emulate the pre-G4 file state:
    db = Database(tmp_path / "h.db")  # no clock attached
    db.conn.execute("INSERT INTO clock_session VALUES(1,?,?,?)",
                    (ANCHOR, ANCHOR, ANCHOR))
    clock = ScenarioClock(ANCHOR)
    clock._db = db
    clock._saved = ANCHOR
    db.conn.execute("DROP TRIGGER clock_session_no_delete")
    db.conn.execute("DELETE FROM clock_session")
    with pytest.raises(RuntimeError, match="clock session row missing"):
        clock._persist("2026-09-14T09:00:00+08:00")


# ---------------- A2': shape guard against hollow shells -----------------

def test_hollow_preplanted_trigger_refuses_construction(tmp_path):
    path = tmp_path / "hollow.db"
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE clock_session (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            scenario_anchor TEXT NOT NULL,
            real_wall_started_at TEXT NOT NULL,
            last_issued_scenario_time TEXT NOT NULL
        );
        CREATE TRIGGER clock_session_no_delete BEFORE DELETE ON clock_session BEGIN
            SELECT 1; -- hollow shell
        END;
    """)
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="missing or tampered"):
        Database(path, clock=ScenarioClock(ANCHOR))


def test_clean_resume_still_passes_guard(tmp_path):
    # Honest restart path: construct, stamp forward, close, reopen —
    # 50->51 style resume must keep working with the guard installed.
    p = tmp_path / "r.db"
    db = Database(p, clock=ScenarioClock(ANCHOR))
    first = db._clock.now()
    db.close()
    db2 = Database(p, clock=ScenarioClock(ANCHOR))
    second = db2._clock.now()
    assert second >= first
    assert db2.conn.execute(
        "SELECT COUNT(*) AS n FROM clock_session").fetchone()["n"] == 1
    db2.close()


# ---------------- G4 1.0.1 pins (reviewer Batch-A gaps) -------------------

def test_rename_column_is_not_aborted_but_next_open_fails_closed(tmp_path):
    # Honest semantics pin (1.0.1-#2): ALTER ... RENAME COLUMN is DDL —
    # the BEFORE triggers NEVER fire on it, and SQLite rewrites the
    # stored trigger SQL to follow the column. What fails closed is the
    # NEXT construction: the canonical shape digest no longer matches
    # the rewritten body. (The old tasks wording claimed rename itself
    # aborts — that was false; this test pins the truth.)
    p = tmp_path / "rz.db"
    db = Database(p, clock=ScenarioClock(ANCHOR))
    db.close()
    raw = sqlite3.connect(str(p))
    raw.execute("ALTER TABLE clock_session RENAME COLUMN"
                " scenario_anchor TO scenario_anchor_changed")
    raw.commit()
    stored = raw.execute(
        "SELECT sql FROM sqlite_master"
        " WHERE name='clock_session_anchor_immutable'").fetchone()[0]
    raw.close()
    assert "scenario_anchor_changed" in stored  # SQLite rewrote the body
    with pytest.raises(RuntimeError, match="missing or tampered"):
        Database(p, clock=ScenarioClock(ANCHOR))


def test_failed_open_releases_handle_no_gc(tmp_path):
    # 1.0.1-#4: after ANY refused construction the sqlite handle must be
    # closed BY the wrapper, not left to CPython refcounting — on Windows
    # a leaked handle makes the file undeletable/unrenameable forever.
    # Rename immediately after the RuntimeError: must succeed.
    p = tmp_path / "leak.db"
    db = Database(p, clock=ScenarioClock(ANCHOR))
    db.close()
    raw = sqlite3.connect(str(p))
    raw.execute("ALTER TABLE clock_session RENAME COLUMN"
                " scenario_anchor TO scenario_anchor_changed")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError):
        Database(p, clock=ScenarioClock(ANCHOR))
    moved = tmp_path / "leak.db.moved"
    os.rename(str(p), str(moved))  # no gc.collect(): handle is gone
    assert moved.exists()


def test_fingerprint_does_not_strip_text_inside_strings():
    # 1.0.1-#5: the fingerprint must not regex-delete "IF NOT EXISTS"
    # anywhere — two bodies differing ONLY inside a RAISE error STRING
    # must stay distinct. (sqlite_master never stores the structural
    # IF NOT EXISTS, so no token strip is needed at all; layout drift
    # and one optional trailing semicolon remain forgiven.)
    plain = ("CREATE TRIGGER t BEFORE DELETE ON x BEGIN"
             " SELECT RAISE(ABORT, 'plain'); END")
    tricky = ("CREATE TRIGGER t BEFORE DELETE ON x BEGIN"
              " SELECT RAISE(ABORT, 'plain IF NOT EXISTS'); END")
    assert _sql_fingerprint(plain) != _sql_fingerprint(tricky)
    layout = ("CREATE TRIGGER t\n    BEFORE DELETE ON x BEGIN\n"
              "        SELECT RAISE(ABORT, 'plain');\n    END;")
    assert _sql_fingerprint(layout) == _sql_fingerprint(plain)
