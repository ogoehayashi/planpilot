"""Authenticated API for independently validated V1.8 planning."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import datetime
import json
import logging
import os
import socket
import sqlite3
import time
from pathlib import Path
import threading
from urllib.parse import urlparse, parse_qs

from planpilot.domain.importer import read_factory
from planpilot.factory_state import FactoryStateRegistry
from planpilot.observability import METRICS, Timer
from planpilot.authority import RuntimeAuthority
from planpilot.approval import ApprovalError
from planpilot.clock import Clock, WallClock, clock_from_env
from planpilot.persistence import Database
from planpilot.publisher import PublisherService
from planpilot.startup_config import (StartupConfigError, config_provenance,
                                      startup_or_die)
from planpilot.runtime_planning import generate_authoritative_plans_from_state
from planpilot.security import authenticate
from planpilot.store import StoreError
from planpilot.tools.middleware import ToolErrorMiddleware
from planpilot.validation.schema import validate_tool_payload
from planpilot.workflow.events import EventWorkflow
from planpilot.agent.chat import ChatService
from planpilot.inference.bedrock_client import BedrockClient, InferenceError
from planpilot.audit import AuditTrail, DecisionTraceWriter, SecurityEventService

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger("planpilot.api")
MAX_BODY = 2 * 1024 * 1024

# p2-5 design §6.3: the /publish route's error_code -> HTTP status table.
# The contract defines no HTTP statuses (failure_transport only mandates the
# tool_error envelope); this is a route convention continuation of the
# pre-middleware classification (PermissionError->403, Store/Approval->409,
# schema->400, other->503). Any unmapped code falls through to 503 exactly
# as the old catch-all did. Approval-domain codes are the six APPROVAL_*.
PUBLISH_STATUS_BY_ERROR_CODE = {
    "INVALID_INPUT": 400,
    "VALIDATION_FAILED": 400,
    "POLICY_VIOLATION": 403,
    "IDEMPOTENCY_CONFLICT": 409,
    "PLAN_VERSION_CONFLICT": 409,
    "PLAN_DIGEST_MISMATCH": 409,
    # STATE_NOT_FOUND (PlanNotFoundError / ApprovalStateNotFoundError) is a
    # store-domain code: the old classification sent every StoreError to 409
    # (§6.3 "reproduces today's classifications"). NO_FEASIBLE_PLAN,
    # RATE_LIMITED and SEARCH_ESCALATION_EXHAUSTED cannot occur on this
    # path; unmapped codes still fall through to 503.
    "STATE_NOT_FOUND": 409,
    "APPROVAL_REQUIRED": 409,
    "APPROVAL_REJECTED": 409,
    "APPROVAL_SET_INCOMPLETE": 409,
    "APPROVAL_EXPIRED": 409,
    "APPROVAL_SET_INVALIDATED": 409,
    "APPROVAL_WINDOW_CLOSED": 409,
    "INTERNAL_ERROR": 503,
    "DEADLINE_EXCEEDED": 503,
}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR can allow competing listeners on the same endpoint.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, db, secret, factory_root, clock: Clock | None = None,
                 ready_timeout_ms: int = 1500, backup_root=None, env: str = "development"):
        if not secret or len(secret) < 32:
            raise ValueError("PLANPILOT_AUTH_SECRET must contain at least 32 characters")
        self.db, self.secret = db, secret
        # [BATCH-A P1-3] env picks the deep overall_ok policy (§4):
        # missing backup manifest and an unconfigured agent are hard
        # failures only in production; the Server stores what main()
        # resolved from the startup snapshot — no second env read here.
        self.env = env
        # G4 Phase 3 (design §4): the readiness budget is ONE value from
        # the startup snapshot (ready_timeout_ms; connect timeout AND
        # busy_timeout share it — see _probe_ready). backup_root feeds
        # /health/deep's manifest lookup ONLY (never mtime guessing).
        self.ready_timeout_ms = int(ready_timeout_ms)
        self.backup_root = Path(backup_root) if backup_root else None
        # Server-owned clock — the ONE time source for lifecycle, approval,
        # audit chain, decision trace, security events and factory state.
        # G1.0.2: injection goes through db.bind_clock(), which is read-only
        # afterwards and fails closed on a conflicting second clock. A
        # ScenarioClock binds its durable session here (restart-monotonic).
        self.clock = clock or db.clock
        db.bind_clock(self.clock)
        self.authority = RuntimeAuthority(db, self.clock)
        self.factory_states = FactoryStateRegistry(db)
        self.security_events = SecurityEventService(db)
        # p2-5 (design §6.1): ONE shared middleware + publisher per server.
        # The observer is the P0-5 DecisionTraceWriter — one decision_trace
        # row per execute (success AND failure), exceptions swallowed by
        # _finish: observability never changes outcomes.
        self.publisher = PublisherService(db, self.authority)
        self.middleware = ToolErrorMiddleware(
            observer=DecisionTraceWriter(AuditTrail(db))
        )
        self.factory_root = Path(factory_root).resolve()
        self.scheduling = threading.BoundedSemaphore(1)
        self.inference = BedrockClient(db)
        self.chat = ChatService(
            db, self.inference, self.factory_root, self.authority, self.security_events
        )
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self._wire(raw, status)

    def _wire(self, raw: bytes, status=200):
        # p2-5 (§6.3): write middleware-produced bytes UNMODIFIED — the
        # envelope (success or tool_error) is already contract-validated
        # and canonically serialised by json_wire/failure_wire. Re-dumping
        # here would break the byte-identical replay guarantee.
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self, permission):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise PermissionError("Bearer token required")
        return authenticate(header[7:], self.server.secret, permission)

    # ---------------- G4 Phase 3 probes (design §4) ----------------------

    def _probe_ready(self):
        """The honest readiness protocol, AS AN ISOLATED FUNCTION so the
        negctl can mutate ONLY this seam. Three claims, all probed:
        (1) the file EXISTS and opens RW — a file: URI with mode=rw
        refuses to create a missing path (Batch A P1-1: plain
        sqlite3.connect once false-greened a zero-byte phantom DB);
        (2) the WRITER reservation is acquirable and releasable — BEGIN
        IMMEDIATE then ROLLBACK, one shared budget ready_timeout_ms for
        connect AND busy_timeout; (3) writes are actually POSSIBLE — an
        in-transaction CREATE falsifier, because sqlite empirically
        ACCEPTS the reservation on a read-only file and only denies the
        write (rev.3->4 P0-1). Zero business writes, zero clock.now()
        (ScenarioClock.now() PERSISTS to clock_session — readiness could
        stall behind the very lock probed). Claim kept honest: exists +
        RW + writer reservation + write-capable — NOT durability, NOT
        free space. Returns (ok, detail)."""
        budget = self.server.ready_timeout_ms / 1000.0
        path = str(self.server.db.path)
        conn = None
        try:
            # [BATCH-A P1-1 fix, reviewer-probed + re-probed this batch]
            # plain sqlite3.connect CREATES a missing file — readiness
            # once false-greened a zero-byte phantom DB. Open the
            # EXISTING file only: file: URI with mode=rw refuses to
            # create AND refuses a vanished path (OperationalError ->
            # 503), while staying equivalent to the default RW open on
            # an existing file.
            uri = Path(path).resolve().as_uri() + "?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=budget)
            conn.execute(f"PRAGMA busy_timeout={self.server.ready_timeout_ms}")
            conn.execute("BEGIN IMMEDIATE")
            # [implementation note, design §4 test-(b)] BEGIN IMMEDIATE
            # alone is NOT enough: empirically (Windows probe, this batch)
            # sqlite ACCEPTS the reservation on a read-only file and only
            # denies the WRITE — the exact rev.3 false-green shape. The
            # in-transaction CREATE is the writability falsifier; DDL on
            # a probe-only table inside an uncommitted txn leaves zero
            # residue after ROLLBACK (pinned by unit test), and it is
            # neither an INSERT nor an UPDATE (§4(c)).
            conn.execute("CREATE TABLE _pp_ready_probe(x)")
            conn.execute("ROLLBACK")
            return True, "ok"
        except Exception as exc:
            # Diagnosis carries the sqlite reason ("database is locked",
            # "attempt to write a readonly database", "file is not a
            # database") truncated and path-redacted — operators get a
            # real cause, tokens/paths never leak through this surface.
            reason = str(exc).replace(path, "<db>")[:120]
            return False, f"error:{type(exc).__name__}: {reason}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _handle_ready(self):
        ok, detail = self._probe_ready()
        if ok:
            self._json({"status": "ok", "service": "planpilot",
                        "probe": "ready", "db": detail})
        else:
            self._json({"status": "not_ready", "service": "planpilot",
                        "probe": "ready", "db": detail}, 503)

    def _handle_deep(self):
        # Repo convention: missing/weak token => PermissionError => 403
        # (same seam every protected route uses; no invented 401).
        self._auth("plan")
        server = self.server
        is_prod = getattr(server, "env", "development") == "production"
        checks = {}
        gate_failures = []  # the ONE overall_ok source — every check lands here
        try:
            ok = bool(server.db.verify_audit())
            checks["audit_chain_ok"] = ok
            if not ok:
                gate_failures.append("audit_chain")
        except Exception as exc:
            checks["audit_chain_ok"] = False
            checks["audit_error"] = type(exc).__name__
            gate_failures.append("audit_chain")
        try:
            with server.db.lock:
                head = server.db.conn.execute(
                    "SELECT entry_count,event_hash FROM audit_chain_head "
                    "WHERE singleton=1").fetchone()
                idem = server.db.conn.execute(
                    "SELECT COUNT(*) AS n FROM idempotency_registry").fetchone()
            checks["audit_head"] = {
                "entry_count": head["entry_count"] if head else 0,
                "head_hash": head["event_hash"] if head else "0" * 64}
            if idem is None:
                checks["idempotency_error"] = "missing_row"
                gate_failures.append("idempotency")
            else:
                checks["idempotency_rows"] = idem["n"]
        except Exception as exc:
            checks["audit_head"] = None
            checks["idempotency_error"] = type(exc).__name__
            gate_failures.append("idempotency")
        try:
            checks["clock"] = server.clock.status()
            # clock.status() carries no ok/fatal field by shape (wall and
            # scenario both render fine or RAISE — e.g. the G4 A1
            # anchor-missing RuntimeError); a raise is the abnormal case
            # and lands in gate_failures via the except below.
        except Exception as exc:
            checks["clock"] = {"error": type(exc).__name__}
            gate_failures.append("clock")
        try:
            configured = bool(server.inference.status().get(
                "configured", False))
            checks["agent_configured"] = configured
            # [BATCH-A P1-3 semantics] NOT configured is a real failure in
            # production; in development the local fallback is expected,
            # so it warns without flipping overall_ok. A status() that
            # RAISES is different — always a failure, any env (except
            # below).
            if not configured and is_prod:
                gate_failures.append("agent")
        except Exception as exc:
            checks["agent_configured"] = False
            checks["agent_error"] = type(exc).__name__
            gate_failures.append("agent")
        # Backup age comes from the V3 manifest ONLY — NEVER a newest-file
        # mtime guess (P1-4). The stamp is the Phase 4 field `verified_at`
        # (RFC3339; epoch floats accepted only with an explicit _epoch
        # suffix) — reading a non-existent key would clock every backup at
        # ~age 0, the false green this seam exists to avoid (Batch A P1-3).
        manifest = None
        if server.backup_root is not None:
            try:
                mf = server.backup_root / "last_verified_backup.json"
                if mf.is_file():
                    manifest = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                manifest = None
        if not isinstance(manifest, dict) or not manifest.get("path"):
            checks["backup"] = {"state": "no_verified_manifest"}
            if is_prod:
                gate_failures.append("backup")
        elif self._manifest_verified_epoch(manifest) is None:
            checks["backup"] = {"state": "manifest_unreadable",
                                "verified_at": manifest.get("verified_at")}
            gate_failures.append("backup")
        else:
            age = max(0.0, time.time() - self._manifest_verified_epoch(manifest))
            checks["backup"] = {"state": "manifest",
                                "age_seconds": round(age, 3),
                                "sha256": manifest.get("sha256")}
        # [BATCH-A P1-3] ONE semantic: every gate check above appends to
        # gate_failures; there is no second core formula. HTTP stays 200
        # in EVERY case — deep is DIAGNOSTICS and must never be a
        # container/gate probe target (§4).
        body = {"overall_ok": not gate_failures, "service": "planpilot",
                "probe": "deep", "checks": checks,
                "gate_failures": gate_failures}
        self._json(body)

    @staticmethod
    def _manifest_verified_epoch(manifest):
        """RFC3339 `verified_at` (the Phase 4 §5.1 field) -> unix seconds.
        An explicit *_epoch float is accepted for callers that store one;
        a missing/unparsable stamp returns None — NEVER age 0. Z suffix
        is swapped before fromisoformat because this venv is Python 3.11
        (bare 'Z' only parses from 3.11+ onward; the swap is belt and
        braces)."""
        raw = manifest.get("verified_at")
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.datetime.fromisoformat(
                    raw.strip().replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        for key in ("verified_at_epoch", "mtime_epoch"):
            val = manifest.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
        return None

    def _handle_publish(self, body):
        # p2-5 (design §4, §6.2): auth stays in the route — a
        # missing/insufficient token is a transport 403, never a
        # tool_error. Everything after auth runs in the shared
        # middleware: input validation happens EXACTLY ONCE inside
        # execute() — for ANY parsed JSON value, including non-objects
        # (reviewer P1: a bare object-guard here would bypass the
        # envelope) — the publisher's PreparedCall holds the §4.5
        # transaction, and the route writes outcome.wire_bytes
        # verbatim: no re-validation, no re-serialisation here.
        principal = self._auth("approve_publish")
        refs = {}
        if isinstance(body, dict):
            for field in ("plan_id", "approval_set_id",
                          "plan_digest", "idempotency_key"):
                value = body.get(field)
                if isinstance(value, str):
                    refs[field] = value
            version = body.get("expected_plan_version")
            if isinstance(version, int) and not isinstance(version, bool):
                refs["plan_version"] = str(version)
        outcome = self.server.middleware.execute(
            "publish_plan",
            body,
            self.server.publisher.prepared_call(actor=principal["sub"]),
            entity_references=refs,
        )
        status = 200
        if outcome.is_error:
            status = PUBLISH_STATUS_BY_ERROR_CODE.get(
                outcome.payload.get("error_code"), 503
            )
        self._wire(outcome.wire_bytes, status)

    def do_GET(self):
        with Timer("http_get"):
            try:
                route = urlparse(self.path)
                if route.path == "/health":
                    with self.server.db.lock:
                        self.server.db.conn.execute("SELECT 1").fetchone()
                    self._json({"status": "ok", "service": "planpilot"})
                elif route.path == "/health/live":
                    # G4 3.1 (design §4): process-level liveness ONLY —
                    # zero DB, zero clock.now(). Alive = HTTP answers.
                    self._json({"status": "ok", "service": "planpilot",
                                "probe": "live"})
                elif route.path == "/health/ready":
                    self._handle_ready()
                elif route.path == "/health/deep":
                    self._handle_deep()
                elif route.path == "/clock":
                    # Read-only, server-owned. The UI must show scenario time
                    # as scenario time; expiry never trusts a client stamp.
                    self._json(self.server.clock.status())
                elif route.path == "/metrics":
                    self._json(METRICS.snapshot())
                elif route.path == "/agent/status":
                    self._auth("plan")
                    self._json(self.server.inference.status())
                elif route.path == "/plans":
                    self._auth("plan")
                    params = parse_qs(route.query)
                    result = self.server.authority.get_plan(params["plan_id"][0], int(params["version"][0]) if "version" in params else None)
                    self._json(result if result else {"error": "plan not found"}, 200 if result else 404)
                elif route.path == "/approval/status":
                    self._auth("plan")
                    params = parse_qs(route.query)
                    expected = {"approval_set_id", "plan_id", "plan_version", "plan_digest"}
                    if set(params) != expected or any(len(value) != 1 for value in params.values()):
                        raise ValueError("approval status requires one value for each binding field")
                    request = {key: params[key][0] for key in expected}
                    request["plan_version"] = int(request["plan_version"])
                    validate_tool_payload(request, "check_approval_status", "input_schema",
                                          "check_approval_status_input")
                    snapshot = self.server.authority.check_approval_status(
                        request["approval_set_id"], request["plan_id"],
                        request["plan_version"], request["plan_digest"],
                    )
                    validate_tool_payload(snapshot, "check_approval_status", "output_schema",
                                          "check_approval_status_output")
                    self._json(snapshot)
                elif route.path == "/audit/status":
                    self._auth("plan")
                    with self.server.db.lock:
                        verified = self.server.db.verify_audit()
                        head = self.server.db.conn.execute(
                            "SELECT entry_count,event_hash FROM audit_chain_head WHERE singleton=1"
                        ).fetchone()
                        rows = self.server.db.conn.execute(
                            "SELECT id,record,event_hash FROM audit_chain ORDER BY id DESC LIMIT 12"
                        ).fetchall()
                    self._json({
                        "verified": verified,
                        "entry_count": head["entry_count"] if head else 0,
                        "head_hash": head["event_hash"] if head else "0" * 64,
                        "recent": [{
                            "audit_log_id": f"AUD-{row['id']:012d}",
                            "event": json.loads(row["record"]).get("event"),
                            "plan_id": json.loads(row["record"]).get("plan_id"),
                            "created_at": json.loads(row["record"]).get("created_at"),
                            "event_hash": row["event_hash"],
                        } for row in rows],
                    })
                elif route.path in ("/", "/index.html"):
                    raw = (ROOT / "tools/web/index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                elif route.path == "/web/p1_3.js":
                    raw = (ROOT / "tools/web/p1_3.js").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self._json({"error": "not found"}, 404)
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except (ApprovalError, StoreError) as exc:
                self._json({"error": str(exc), "error_code": exc.code,
                            "details": exc.to_error_details()}, 409)
            except (ValueError, KeyError) as exc:
                self._json({"error": str(exc)}, 400)
            except Exception:
                LOGGER.exception("read request failed")
                self._json({"error": "service unavailable"}, 503)

    def do_POST(self):
        with Timer("http_post"):
            try:
                if self.path not in ("/plan", "/schedule", "/approval/request", "/approval/decide", "/publish", "/agent/chat"):
                    self._json({"error": "not found"}, 404)
                    return
                principal = self._auth("plan")
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY:
                    self._json({"error": "invalid request length"}, 413)
                    return
                if self.headers.get_content_type() != "application/json":
                    self._json({"error": "application/json required"}, 415)
                    return
                body = json.loads(self.rfile.read(length))
                if self.path == "/publish":
                    # p2-5: ANY parsed JSON value (list, string, number,
                    # null included) goes to the middleware — it is the
                    # single contract validation entry and already wraps
                    # non-Mapping payloads (middleware.py:174) into the
                    # INVALID_INPUT envelope + trace. A bare guard here
                    # would bypass the envelope.
                    self._handle_publish(body)
                    return
                if not isinstance(body, dict):
                    raise ValueError("request body must be an object")
                authority = self.server.authority
                if self.path == "/agent/chat":
                    if not self.server.scheduling.acquire(blocking=False):
                        self._json({"error": "scheduler or agent busy"}, 429)
                        return
                    try:
                        self._json(self.server.chat.run(body, principal["sub"]))
                    finally:
                        self.server.scheduling.release()
                elif self.path == "/approval/request":
                    validate_tool_payload(
                        body, "request_approval", "input_schema", "request_approval_input"
                    )
                    self._json(authority.request_approval(
                        body["plan_id"], body["plan_version"], body["plan_digest"],
                        body["action"], principal["sub"], expires_at=body.get("expires_at"),
                    ))
                elif self.path == "/approval/decide":
                    principal = self._auth(authority.required_permission(body["request_id"]))
                    self._json(authority.decide_approval(
                        body["request_id"], body["decision"], principal["sub"],
                        principal["role"], decision_reason=body.get("decision_reason"),
                        decision_comment=body.get("decision_comment"),
                    ))
                else:
                    if not self.server.scheduling.acquire(blocking=False):
                        self._json({"error": "scheduler busy"}, 429)
                        return
                    try:
                        with Timer("schedule"):
                            state_id = body.get("state_id")
                            raw = body.get("factory_data")
                            state_validation = None
                            event_result = None
                            logged_security_events = []
                            if state_id is not None:
                                if not isinstance(state_id, str):
                                    raise ValueError("state_id must be a string")
                                record = self.server.factory_states.get(state_id)
                                raw = record["state"]
                                state_validation = record["validation"]
                            elif raw is None:
                                relative = Path(body["factory_file"])
                                path = (self.server.factory_root / relative).resolve()
                                if not path.is_relative_to(self.server.factory_root):
                                    raise PermissionError("factory file is outside configured import directory")
                                if path.suffix.lower() == ".xlsx":
                                    loaded = self.server.factory_states.load_workbook(path)
                                    state_id = loaded["state_id"]
                                    loaded_record = self.server.factory_states.get(state_id)
                                    state_validation = loaded_record["validation"]
                                    logged_security_events.extend(
                                        self.server.security_events.log_factory_quarantine(loaded_record)
                                    )
                                    raw = self.server.factory_states.planning_state(state_id)
                                else:
                                    raw = read_factory(path)
                            event = body.get("event")
                            if body.get("event_id"):
                                event = next((e for e in raw.get("events", []) if e.get("event_id") == body["event_id"]), None)
                                if event is None:
                                    raise ValueError("event_id is not present in the loaded Events data")
                            if event:
                                workflow = EventWorkflow(raw, self.server.security_events)
                                event_result = workflow.replan(event)
                                raw = workflow.factory_data
                                state_id = None
                                if event_result["security_events"]:
                                    issue = {
                                        "code": "PROMPT_INJECTION", "severity": "ERROR",
                                        "entity_type": "event", "entity_id": event.get("event_id"),
                                        "field": "Payload_JSON",
                                        "message": "untrusted instruction-like payload was quarantined",
                                    }
                                    previous = state_validation or {
                                        "status": "VALID", "errors": [], "warnings": [],
                                        "quarantined_entities": [],
                                    }
                                    state_validation = {
                                        "status": "VALID_WITH_QUARANTINE",
                                        "errors": [*previous["errors"], issue],
                                        "warnings": list(previous["warnings"]),
                                        "quarantined_entities": [*previous["quarantined_entities"], issue],
                                    }
                            if state_id is None:
                                state_id = self.server.factory_states.register_normalized(
                                    raw, validation=state_validation
                                )["state_id"]
                            response = generate_authoritative_plans_from_state(
                                state_id, self.server.factory_states, authority
                            )
                            if event_result and event_result["security_events"]:
                                logged_security_events.extend(event_result["security_events"])
                            if logged_security_events:
                                response["security_events"] = logged_security_events
                            self._json(response)
                    finally:
                        self.server.scheduling.release()
            except InferenceError as exc:
                self._json({"error": str(exc)}, 502)
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except (ApprovalError, StoreError) as exc:
                self._json({"error": str(exc), "error_code": exc.code,
                            "details": exc.to_error_details()}, 409)
            except (ValueError, KeyError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)
            except Exception:
                LOGGER.exception("request failed")
                self._json({"error": "internal server error"}, 500)

    def log_message(self, fmt, *args):
        LOGGER.info("%s %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=logging.INFO)
    # G4 Phase 2 (design §3): read the environment exactly ONCE; every
    # startup field comes from that snapshot via the pure parser + the
    # I/O preflight. A refusal means ZERO Database/Clock construction and
    # ZERO sockets — same shape the Phase 4 lock gate will use.
    # (Per the Batch-A review the exception is exactly TWO runtime reads:
    # the Bedrock credential and the LLM-network switch, both re-read per
    # call inside bedrock_client for rotation; region/model/daily limit
    # are read once when BedrockClient is constructed. None of them
    # enters StartupConfig.)
    snapshot = dict(os.environ)
    try:
        cfg, warnings = startup_or_die(snapshot)
    except StartupConfigError as exc:
        raise SystemExit(f"startup config refused: {exc}") from exc
    # G4 2.3: log WHERE config came from, once, redacted — non-sensitive
    # config values and paths are fine to show; the secret itself only
    # ever appears as present/length, never its value.
    LOGGER.info("startup config provenance: %s", json.dumps(
        config_provenance(cfg), sort_keys=True))
    for w in warnings:
        print(f"WARNING: {w}", flush=True)
    # G1.0.1 fail-safe preserved: unset/invalid PLANPILOT_CLOCK_MODE means
    # REAL wall time; scenario on a public bind was already refused in
    # parse. Local demo: PLANPILOT_CLOCK_MODE=scenario (explicit).
    clock = clock_from_env(snapshot, cfg.host)
    # [BATCH-A review-3 #1] BOTH constructors live inside the try: if
    # Server(...) raises (a bind error, say), the already-open Database
    # handle still closes in the finally — previously it leaked.
    db = None
    server = None
    try:
        db = Database(cfg.db_path, clock=clock)
        if clock.kind == "scenario":
            print(f"WARNING: scenario clock active ({clock.now()}) — demo "
                  "data only, NOT production time. Audit and approval "
                  "stamps share it.", flush=True)
        server = Server((cfg.host, cfg.port), db, cfg.auth_secret,
                        cfg.factory_root, clock=clock,
                        ready_timeout_ms=cfg.ready_timeout_ms,
                        backup_root=cfg.backup_dir, env=cfg.env)
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
