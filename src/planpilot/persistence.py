"""SQLite infrastructure for runtime snapshots, audit and inference metering.

Business authority deliberately does not live here.  Plan content/lifecycle is
owned by :class:`planpilot.store.PlanStore`; approvals are owned by
:class:`planpilot.approval.ApprovalService`.  ``RuntimeAuthority`` persists the
validated state of both services as one optimistic SQLite transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def now():
    """Module default: real wall time. Database instances carry a server-owned
    Clock instead (G1.0.1) so audit/trace/state stamps agree with lifecycle
    stamps; do not add new authoritative writers that call this directly."""
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


class Database:
    """Infrastructure database; contains no plan or approval business rules.

    The optional ``clock`` is the single server-owned time source for every
    authoritative stamp written through this instance (audit chain rows,
    factory-state rows, inference-day rollovers). Test/demo callers pass a
    FixedClock/ScenarioClock; production defaults to WallClock semantics.
    """

    def __init__(self, path="planpilot.db", clock=None):
        clock_given = clock is not None
        if clock is None:
            from .clock import WallClock
            clock = WallClock()
        # G1.0.2: the clock is bound ONCE here and is read-only afterwards
        # (review P2: "Database holds the one clock" was only a convention
        # while RuntimeAuthority/Server could reassign database.clock).
        # The composition root (Server) may upgrade a plain wall clock to a
        # scenario clock exactly once via bind_clock(); a second, DIFFERENT
        # clock fails closed.
        self._clock = clock
        self._clock_kind_explicit = clock_given
        # A scenario clock needs its durable session; attach AFTER the schema
        # exists (executescript below creates clock_session). WallClock and
        # FixedClock ignore this hook.
        self._pending_clock_attach = getattr(clock, "attach_database", None)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(path), check_same_thread=False, timeout=10, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS authority_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                revision INTEGER NOT NULL,
                plan_store_json TEXT NOT NULL,
                approval_service_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_chain (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_chain_head (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                entry_count INTEGER NOT NULL,
                event_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clock_session (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                scenario_anchor TEXT NOT NULL,
                real_wall_started_at TEXT NOT NULL,
                last_issued_scenario_time TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS audit_chain_no_update
            BEFORE UPDATE ON audit_chain BEGIN
                SELECT RAISE(ABORT, 'audit_chain is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_chain_no_delete
            BEFORE DELETE ON audit_chain BEGIN
                SELECT RAISE(ABORT, 'audit_chain is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_chain_advance_head
            AFTER INSERT ON audit_chain BEGIN
                INSERT INTO audit_chain_head(singleton,entry_count,event_hash)
                VALUES(1,(SELECT COUNT(*) FROM audit_chain),NEW.event_hash)
                ON CONFLICT(singleton) DO UPDATE SET
                    entry_count=excluded.entry_count,
                    event_hash=excluded.event_hash;
            END;
        """)
        tail = self.conn.execute(
            "SELECT COUNT(*) AS n,COALESCE((SELECT event_hash FROM audit_chain ORDER BY id DESC LIMIT 1),?) AS head FROM audit_chain",
            ("0" * 64,),
        ).fetchone()
        self.conn.execute(
            "INSERT OR IGNORE INTO audit_chain_head VALUES(1,?,?)",
            (tail["n"], tail["head"]),
        )
        if self._pending_clock_attach is not None:
            self._pending_clock_attach(self)
            self._pending_clock_attach = None

    @property
    def clock(self):
        """The server-owned time source. Read-only by construction (G1.0.2)."""
        return self._clock

    def bind_clock(self, clock):
        """Composition-root injection point, usable exactly once.

        A Database built without an explicit clock boots on a default
        WallClock; the Server (the only composition root) may bind its clock
        there once. A Database that already carries an explicit clock is
        authoritative as-is, and binding a DIFFERENT clock fails closed —
        two time sources in one process was the dual-clock bug class.

        The attach is validated BEFORE any state commits (G1.0.2 follow-up):
        a clock whose session conflicts (foreign anchor) raises and leaves
        this Database exactly as it was — never a half-bound foreign clock
        that RuntimeAuthority would then accept.
        """
        with self.lock:
            if clock is self._clock:
                return self._clock
            if self._clock_kind_explicit:
                raise RuntimeError(
                    f"database clock {self._clock.kind!r} is already bound; "
                    f"refusing to swap in {clock.kind!r} (fail-closed: one "
                    "process, one time source)")
            attach = getattr(clock, "attach_database", None)
            if attach is not None:
                attach(self)
            self._clock = clock
            self._clock_kind_explicit = True
            return self._clock

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def close(self):
        with self.lock:
            self.conn.close()

    def _append_audit_record(self, value):
        row = self.conn.execute(
            "SELECT event_hash FROM audit_chain ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row[0] if row else "0" * 64
        record = canonical(value)
        hashed = hashlib.sha256((previous + record).encode("utf-8")).hexdigest()
        cursor = self.conn.execute(
            "INSERT INTO audit_chain(record,previous_hash,event_hash) VALUES(?,?,?)",
            (record, previous, hashed),
        )
        return {"audit_log_id": f"AUD-{cursor.lastrowid:012d}",
                "previous_hash": previous, "event_hash": hashed, "record": record}

    def _audit(self, plan_id, actor, event, payload):
        return self._append_audit_record({
            "plan_id": plan_id,
            "actor": actor,
            "event": event,
            "payload": payload,
            "created_at": self.clock.now(),
        })

    def audit(self, plan_id, actor, event, payload):
        with self.transaction():
            self._audit(plan_id, actor, event, payload)

    def verify_audit(self):
        with self.lock:
            previous = "0" * 64
            count = 0
            for row in self.conn.execute("SELECT * FROM audit_chain ORDER BY id"):
                count += 1
                expected = hashlib.sha256((previous + row["record"]).encode("utf-8")).hexdigest()
                if row["id"] != count or row["previous_hash"] != previous or row["event_hash"] != expected:
                    return False
                previous = row["event_hash"]
            head = self.conn.execute(
                "SELECT entry_count,event_hash FROM audit_chain_head WHERE singleton=1"
            ).fetchone()
            return bool(head and head["entry_count"] == count and head["event_hash"] == previous)
