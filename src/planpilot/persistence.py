"""Transactional adapter store. Immutable revisions and approvals survive restart."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading
import uuid


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


ROLES = {"publish_plan": "planner", "add_overtime": "manager",
         "change_promised_due_date": "manager", "assign_qualified_secondary_skill": "planner"}
PERMISSIONS = {"publish_plan": "approve_publish", "add_overtime": "approve_overtime",
               "change_promised_due_date": "approve_due_date", "assign_qualified_secondary_skill": "approve_secondary"}


class Database:
    def __init__(self, path="planpilot.db"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions (
                plan_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
                digest TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(plan_id,version));
            CREATE TABLE IF NOT EXISTS bound_approvals (
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, version INTEGER NOT NULL,
                digest TEXT NOT NULL, candidate_id TEXT NOT NULL, action TEXT NOT NULL,
                role TEXT NOT NULL, status TEXT NOT NULL, expires_at TEXT NOT NULL,
                decided_by TEXT, decided_at TEXT,
                UNIQUE(plan_id,version,candidate_id,action));
            CREATE TABLE IF NOT EXISTS publications (
                plan_id TEXT NOT NULL, version INTEGER NOT NULL, candidate_id TEXT NOT NULL,
                digest TEXT NOT NULL, actor TEXT NOT NULL, published_at TEXT NOT NULL,
                PRIMARY KEY(plan_id,version,candidate_id));
            CREATE TABLE IF NOT EXISTS audit_chain (
                id INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL,
                previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
        """)
        # Retain legacy heads as revisions. Legacy unbound approvals are never trusted.
        with self.transaction():
            for row in self.conn.execute("SELECT * FROM plans").fetchall():
                self.conn.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?)",
                                  (row["id"], row["version"], row["payload"], digest(json.loads(row["payload"])), row["updated_at"]))

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

    def _audit(self, plan_id, actor, event, payload):
        row = self.conn.execute("SELECT event_hash FROM audit_chain ORDER BY id DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        record = canonical({"plan_id": plan_id, "actor": actor, "event": event, "payload": payload, "created_at": now()})
        hashed = hashlib.sha256((previous + record).encode()).hexdigest()
        self.conn.execute("INSERT INTO audit_chain(record,previous_hash,event_hash) VALUES(?,?,?)", (record, previous, hashed))

    def audit(self, plan_id, actor, event, payload):
        with self.transaction():
            self._audit(plan_id, actor, event, payload)

    def verify_audit(self):
        with self.lock:
            previous = "0" * 64
            for row in self.conn.execute("SELECT * FROM audit_chain ORDER BY id"):
                if row["previous_hash"] != previous or row["event_hash"] != hashlib.sha256((previous + row["record"]).encode()).hexdigest():
                    return False
                previous = row["event_hash"]
            return True

    def save_plan(self, plan_id, payload, version, actor="system"):
        if not isinstance(plan_id, str) or not plan_id or type(version) is not int or version < 1:
            raise ValueError("invalid plan id or version")
        encoded, hashed = canonical(payload), digest(payload)
        with self.transaction():
            current = self.conn.execute("SELECT version FROM plans WHERE id=?", (plan_id,)).fetchone()
            if (current[0] if current else 0) != version - 1:
                raise RuntimeError("optimistic concurrency conflict")
            timestamp = now()
            self.conn.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (plan_id, version, encoded, hashed, timestamp))
            self.conn.execute("INSERT INTO plans VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,version=excluded.version,updated_at=excluded.updated_at",
                              (plan_id, encoded, version, timestamp))
            self.conn.execute("UPDATE bound_approvals SET status='INVALIDATED' WHERE plan_id=? AND version<>? AND status IN ('PENDING','APPROVED')", (plan_id, version))
            self._audit(plan_id, actor, "plan_saved", {"version": version, "digest": hashed})
        return {"plan_id": plan_id, "version": version, "digest": hashed}

    def get_plan(self, plan_id, version=None):
        with self.lock:
            if version is None:
                head = self.conn.execute("SELECT version FROM plans WHERE id=?", (plan_id,)).fetchone()
                if not head:
                    return None
                version = head[0]
            row = self.conn.execute("SELECT * FROM revisions WHERE plan_id=? AND version=?", (plan_id, version)).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            if digest(payload) != row["digest"]:
                raise RuntimeError("plan digest mismatch")
            return {"payload": payload, "version": row["version"], "digest": row["digest"], "updated_at": row["created_at"]}

    def _candidate(self, plan_id, candidate_id, expected_version=None):
        plan = self.get_plan(plan_id)
        if plan is None:
            raise KeyError("plan not found")
        if expected_version is not None and plan["version"] != expected_version:
            raise RuntimeError("optimistic concurrency conflict")
        candidates = [c for c in plan["payload"].get("candidates", []) if c.get("profile") == candidate_id]
        if len(candidates) != 1:
            raise ValueError("candidate not found or ambiguous")
        candidate = candidates[0]
        if candidate.get("violations") or not candidate.get("operations"):
            raise PermissionError("candidate is not validated and feasible")
        # Only a persisted source can establish feasibility, never a client flag.
        from .domain.importer import factory_from_dict
        from .domain.planning import validate_plan, reserve_materials
        factory = factory_from_dict(plan["payload"]["factory_data"])
        if validate_plan(candidate["operations"], factory, candidate.get("unscheduled_operations", [])):
            raise PermissionError("independent validation failed")
        if candidate.get("material_reservations") != reserve_materials(factory):
            raise PermissionError("material reservation mismatch")
        actions = {"publish_plan"}
        from .domain.planning import is_secondary
        scheduled = {(r["order_id"], r["operation_no"]) for r in candidate["operations"]}
        if any(is_secondary(factory, op) for order in factory.orders for op in order.operations if (order.order_id, op.operation_no) in scheduled):
            actions.add("assign_qualified_secondary_skill")
        if candidate.get("kpis", {}).get("overtime_hours", 0) > 0:
            actions.add("add_overtime")
        if plan["payload"].get("change_promised_due_date"):
            actions.add("change_promised_due_date")
        return plan, actions

    def request_approval(self, plan_id, candidate_id, actions=None, expected_version=None, actor="system"):
        with self.transaction():
            plan, required = self._candidate(plan_id, candidate_id, expected_version)
            if actions is not None and set(actions) != required:
                raise ValueError("approval actions must match server-derived requirements")
            expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            for action in sorted(required):
                self.conn.execute("INSERT OR IGNORE INTO bound_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                  (str(uuid.uuid4()), plan_id, plan["version"], plan["digest"], candidate_id, action, ROLES[action], "PENDING", expiry, None, None))
            result = [dict(row) for row in self.conn.execute("SELECT *,id AS request_id FROM bound_approvals WHERE plan_id=? AND version=? AND candidate_id=? ORDER BY action", (plan_id, plan["version"], candidate_id))]
            self._audit(plan_id, actor, "approval_requested", {"version": plan["version"], "candidate_id": candidate_id})
            return result

    def decide_approval(self, request_id, actor, role, decision):
        if decision not in ("APPROVED", "REJECTED"):
            raise ValueError("decision must be APPROVED or REJECTED")
        with self.transaction():
            row = self.conn.execute("SELECT * FROM bound_approvals WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError("approval request not found")
            plan = self.get_plan(row["plan_id"])
            if plan["version"] != row["version"] or plan["digest"] != row["digest"]:
                raise RuntimeError("stale approval")
            if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                raise PermissionError("approval expired")
            if row["role"] != role:
                raise PermissionError("role cannot decide this request")
            if row["status"] != "PENDING":
                if row["status"] == decision and row["decided_by"] == actor:
                    return {"request_id": request_id, "status": decision, "decided_by": actor}
                raise RuntimeError("approval decision conflict")
            self.conn.execute("UPDATE bound_approvals SET status=?,decided_by=?,decided_at=? WHERE id=?", (decision, actor, now(), request_id))
            self._audit(row["plan_id"], actor, "approval_decided", {"request_id": request_id, "decision": decision})
            return {"request_id": request_id, "status": decision, "decided_by": actor}

    def approval_permission(self, request_id):
        with self.lock:
            row = self.conn.execute("SELECT action FROM bound_approvals WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError("approval request not found")
            return PERMISSIONS[row[0]]

    def publish_plan(self, plan_id, candidate_id, actor, role, expected_version=None):
        if role != "planner":
            raise PermissionError("only planner may publish")
        with self.transaction():
            plan, required = self._candidate(plan_id, candidate_id, expected_version)
            identity = (plan_id, plan["version"], candidate_id)
            published = self.conn.execute("SELECT * FROM publications WHERE plan_id=? AND version=? AND candidate_id=?", identity).fetchone()
            if published:
                return {**dict(published), "status": "PUBLISHED"}
            rows = self.conn.execute("SELECT * FROM bound_approvals WHERE plan_id=? AND version=? AND candidate_id=?", identity).fetchall()
            if {r["action"] for r in rows} != required or any(r["status"] != "APPROVED" or r["digest"] != plan["digest"] or datetime.fromisoformat(r["expires_at"]) <= datetime.now(timezone.utc) for r in rows):
                raise PermissionError("all current approval requests must be approved")
            timestamp = now()
            self.conn.execute("INSERT INTO publications VALUES(?,?,?,?,?,?)", (*identity, plan["digest"], actor, timestamp))
            self._audit(plan_id, actor, "plan_published", {"version": plan["version"], "candidate_id": candidate_id, "digest": plan["digest"]})
            return {"plan_id": plan_id, "version": plan["version"], "candidate_id": candidate_id, "digest": plan["digest"], "actor": actor, "published_at": timestamp, "status": "PUBLISHED"}
