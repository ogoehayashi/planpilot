"""Authenticated API for the compact production planning adapter."""
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
from pathlib import Path
import threading
from urllib.parse import urlparse, parse_qs
import uuid

from planpilot.domain.importer import read_factory, factory_from_dict
from planpilot.domain.planning import build_candidates
from planpilot.observability import METRICS, Timer
from planpilot.persistence import Database
from planpilot.security import authenticate
from planpilot.workflow.events import EventWorkflow
from planpilot.agent.chat import ChatService
from planpilot.inference.bedrock_client import BedrockClient, InferenceError

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

    def __init__(self, address, db, secret, factory_root):
        if not secret or len(secret) < 32:
            raise ValueError("PLANPILOT_AUTH_SECRET must contain at least 32 characters")
        self.db, self.secret = db, secret
        self.factory_root = Path(factory_root).resolve()
        self.scheduling = threading.BoundedSemaphore(1)
        self.inference = BedrockClient(db)
        self.chat = ChatService(db, self.inference, self.factory_root)
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
                elif route.path == "/metrics":
                    self._json(METRICS.snapshot())
                elif route.path == "/agent/status":
                    self._auth("plan")
                    self._json(self.server.inference.status())
                elif route.path == "/plans":
                    self._auth("plan")
                    params = parse_qs(route.query)
                    result = self.server.db.get_plan(params["plan_id"][0], int(params["version"][0]) if "version" in params else None)
                    self._json(result if result else {"error": "plan not found"}, 200 if result else 404)
                elif route.path in ("/", "/index.html"):
                    raw = (ROOT / "tools/web/index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self._json({"error": "not found"}, 404)
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
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
                db = self.server.db
                if self.path == "/agent/chat":
                    if not self.server.scheduling.acquire(blocking=False):
                        self._json({"error": "scheduler or agent busy"}, 429)
                        return
                    try:
                        self._json(self.server.chat.run(body, principal["sub"]))
                    finally:
                        self.server.scheduling.release()
                elif self.path == "/approval/request":
                    self._json({"approvals": db.request_approval(body["plan_id"], body["candidate_id"], body.get("actions"), self._version(body), principal["sub"])})
                elif self.path == "/approval/decide":
                    self._auth(db.approval_permission(body["request_id"]))
                    self._json(db.decide_approval(body["request_id"], principal["sub"], principal["role"], body["decision"]))
                elif self.path == "/publish":
                    self._auth("approve_publish")
                    self._json(db.publish_plan(body["plan_id"], body["candidate_id"], principal["sub"], principal["role"], self._version(body)))
                else:
                    if not self.server.scheduling.acquire(blocking=False):
                        self._json({"error": "scheduler busy"}, 429)
                        return
                    try:
                        with Timer("schedule"):
                            raw = body.get("factory_data")
                            if raw is None:
                                relative = Path(body["factory_file"])
                                path = (self.server.factory_root / relative).resolve()
                                if not path.is_relative_to(self.server.factory_root):
                                    raise PermissionError("factory file is outside configured import directory")
                                raw = read_factory(path)
                            event = body.get("event")
                            if body.get("event_id"):
                                event = next((e for e in raw.get("events", []) if e.get("event_id") == body["event_id"]), None)
                                if event is None:
                                    raise ValueError("event_id is not present in the loaded Events data")
                            if event:
                                workflow = EventWorkflow(raw)
                                event_result = workflow.replan(event)
                                if event_result["state"] == "BLOCKED":
                                    self._json({"state": "BLOCKED", "event_id": event.get("event_id"), "security_event": event_result["security_event"], "traces": event_result["traces"]}, 422)
                                    return
                                raw = workflow.factory_data
                            candidates = [asdict(p) for p in build_candidates(factory_from_dict(raw))]
                            plan_id = body.get("plan_id") or str(uuid.uuid4())
                            version = body.get("expected_version", 0)
                            if type(version) is not int or version < 0:
                                raise ValueError("expected_version must be a non-negative integer")
                            saved = db.save_plan(plan_id, {"candidates": candidates, "factory_data": raw}, version + 1, principal["sub"])
                            feasible = [c for c in candidates if c["operations"] and not c["violations"]]
                            best = max(feasible, key=lambda c: (c["kpis"]["eligible_order_coverage_rate"], c["kpis"]["on_time_rate"], -c["kpis"]["total_tardiness_min"])) if feasible else None
                            self._json({**saved, "candidates": candidates, "recommended_plan": best["profile"] if best else None,
                                        "approval_required": bool(best), "publish_ready": False,
                                        "response": "计划已生成；请比较未排工序、延迟订单和物料预留后发起审批。" if best else "没有可执行计划；请检查未排工序和资源约束。"})
                    finally:
                        self.server.scheduling.release()
            except InferenceError as exc:
                self._json({"error": str(exc)}, 502)
            except PermissionError as exc:
                self._json({"error": str(exc)}, 403)
            except (ValueError, KeyError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)
            except Exception:
                LOGGER.exception("request failed")
                self._json({"error": "internal server error"}, 500)

    @staticmethod
    def _version(body):
        value = body["expected_version"]
        if type(value) is not int or value < 1:
            raise ValueError("expected_version must be a positive integer")
        return value

    def log_message(self, fmt, *args):
        LOGGER.info("%s %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=logging.INFO)
    db = Database(os.environ.get("PLANPILOT_DB", "planpilot.db"))
    server = Server((os.environ.get("PLANPILOT_HOST", "127.0.0.1"), int(os.environ.get("PLANPILOT_PORT", "8080"))),
                    db, os.environ.get("PLANPILOT_AUTH_SECRET"), os.environ.get("PLANPILOT_FACTORY_ROOT", ROOT / "examples"))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()
