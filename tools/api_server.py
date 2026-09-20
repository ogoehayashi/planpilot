"""Authenticated API for independently validated V1.8 planning."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
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
from planpilot.runtime_planning import generate_authoritative_plans_from_state
from planpilot.security import authenticate
from planpilot.store import StoreError
from planpilot.validation.schema import validate_tool_payload
from planpilot.workflow.events import EventWorkflow
from planpilot.agent.chat import ChatService
from planpilot.inference.bedrock_client import BedrockClient, InferenceError
from planpilot.audit import SecurityEventService

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger("planpilot.api")
MAX_BODY = 2 * 1024 * 1024


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR can allow competing listeners on the same endpoint.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, db, secret, factory_root, clock: Clock | None = None):
        if not secret or len(secret) < 32:
            raise ValueError("PLANPILOT_AUTH_SECRET must contain at least 32 characters")
        self.db, self.secret = db, secret
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

    def do_GET(self):
        with Timer("http_get"):
            try:
                route = urlparse(self.path)
                if route.path == "/health":
                    with self.server.db.lock:
                        self.server.db.conn.execute("SELECT 1").fetchone()
                    self._json({"status": "ok", "service": "planpilot"})
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
                elif self.path == "/publish":
                    principal = self._auth("approve_publish")
                    validate_tool_payload(
                        body, "publish_plan", "input_schema", "publish_plan_input"
                    )
                    self._json(authority.publish_plan(
                        body["plan_id"], body["expected_plan_version"], body["plan_digest"],
                        body["approval_set_id"], principal["sub"], principal["role"],
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
    # G1.0.1 fail-safe: unset/invalid PLANPILOT_CLOCK_MODE means REAL wall
    # time. A deployment that forgets env config must never run on the fixed
    # 2026-09-14 scenario clock and bless stale plans. Local demo:
    # PLANPILOT_CLOCK_MODE=scenario (explicit; refuses public binds).
    host = os.environ.get("PLANPILOT_HOST", "127.0.0.1")
    try:
        clock = clock_from_env(os.environ, host)
    except ValueError as exc:
        raise SystemExit(f"clock policy refused startup: {exc}") from exc
    db = Database(os.environ.get("PLANPILOT_DB", "planpilot.db"), clock=clock)
    if clock.kind == "scenario":
        print(f"WARNING: scenario clock active ({clock.now()}) — demo data "
              "only, NOT production time. Audit and approval stamps share it.",
              flush=True)
    server = Server((host, int(os.environ.get("PLANPILOT_PORT", "8080"))),
                    db, os.environ.get("PLANPILOT_AUTH_SECRET"), os.environ.get("PLANPILOT_FACTORY_ROOT", ROOT / "data"),
                    clock=clock)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()
