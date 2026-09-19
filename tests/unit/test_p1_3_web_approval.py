"""P1-3: one authenticated HTTP/UI path for selecting, deciding and publishing."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from planpilot.persistence import Database
from planpilot.security import issue_token


ROOT = Path(__file__).resolve().parents[2]
SECRET = "p1-3-test-secret-with-at-least-32-characters"


def test_web_assets_and_http_approval_lifecycle(tmp_path):
    spec = importlib.util.spec_from_file_location("p1_3_api", ROOT / "tools/api_server.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    db = Database(tmp_path / "planpilot.db")
    server = api.Server(("127.0.0.1", 0), db, SECRET, ROOT / "data")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, body=None, role="planner"):
        token = issue_token(role, role, SECRET) if role else ""
        request = Request(base + path,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + token})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def binding(option):
        return {"plan_id": option["plan_id"], "plan_version": option["plan_version"],
                "plan_digest": option["plan_digest"]}

    try:
        with urlopen(base + "/", timeout=10) as response:
            html = response.read().decode()
        with urlopen(base + "/web/p1_3.js", timeout=10) as response:
            script = response.read().decode()
        assert all(marker in html for marker in (
            'id="load"', 'id="managerToken"', 'id="refreshApproval"',
            'id="refreshAudit"', '/web/p1_3.js',
        ))
        assert all(marker in script for marker in (
            '/plans?', '/approval/status?', '/approval/decide', '/audit/status',
            'APPROVED', 'REJECTED', 'plan_digest',
        ))
        assert call("/audit/status", role=None)[0] == 403
        state = json.loads((ROOT / "data/factory_demo_v18.json").read_text())
        status, generation = call("/schedule", {"factory_data": state})
        assert status == 200 and len(generation["plan_options"]) == 3
        option = next(row for row in generation["plan_options"] if row["profile"] != generation["content"]["profile"])
        status, loaded = call("/plans?" + urlencode({"plan_id": option["plan_id"]}))
        assert status == 200 and loaded["content"]["plan_digest"] == option["plan_digest"]

        status, requested = call("/approval/request", {**binding(option), "action": "publish_plan"})
        assert status == 200 and requested["is_complete"]
        assert len(requested["approvals"]) >= 1
        publish_approval = next(row for row in requested["approvals"] if row["action"] == "publish_plan")
        assert publish_approval["impact_summary"]["changed_operation_count"] == len(loaded["content"]["operations"])
        assert publish_approval["impact_summary"]["affected_order_ids"]
        query = urlencode({"approval_set_id": requested["approval_set_id"], **binding(option)})
        assert call("/approval/status?" + query)[1]["aggregate_status"] == "PENDING"
        assert call("/approval/status?" + query, role=None)[0] == 403
        assert call("/approval/decide", {
            "request_id": publish_approval["approval_request_id"], "decision": "APPROVED"
        }, role="manager")[0] == 403
        for row in requested["approvals"]:
            assert row["approver_role"] == "Production Planner"
            status, decided = call("/approval/decide", {
                "request_id": row["approval_request_id"], "decision": "APPROVED"
            })
            assert status == 200
        assert decided["aggregate_status"] == "APPROVED"
        assert call("/approval/status?" + query)[1]["aggregate_status"] == "APPROVED"
        publish = {"plan_id": option["plan_id"], "expected_plan_version": option["plan_version"],
                   "plan_digest": option["plan_digest"], "approval_set_id": requested["approval_set_id"],
                   "idempotency_key": "p1-3-web-publish-0001"}
        assert call("/publish", publish)[1]["status"] == "PUBLISHED"
        audit = call("/audit/status")[1]
        assert audit["verified"] is True
        assert audit["entry_count"] >= 1 and len(audit["head_hash"]) == 64

        rejected_option = next(row for row in generation["plan_options"] if row["plan_id"] != option["plan_id"])
        rejected = call("/approval/request", {**binding(rejected_option), "action": "publish_plan"})[1]
        first_request = rejected["approvals"][0]
        status, rejected_status = call("/approval/decide", {
            "request_id": first_request["approval_request_id"], "decision": "REJECTED",
            "decision_reason": "PLAN_NOT_REVIEWED_YET",
        })
        assert status == 200 and rejected_status["aggregate_status"] == "REJECTED"
        rejected_publish = {**publish, **binding(rejected_option),
                            "expected_plan_version": rejected_option["plan_version"],
                            "approval_set_id": rejected["approval_set_id"]}
        rejected_publish.pop("plan_version")
        status, error = call("/publish", rejected_publish)
        assert status == 409 and error["error_code"] == "APPROVAL_REJECTED"

        call("/schedule", {"factory_data": state})
        stale = call("/approval/status?" + query)[1]
        assert stale["aggregate_status"] == "INVALIDATED"
        status, error = call("/publish", publish)
        assert status == 409 and error["error_code"] == "APPROVAL_SET_INVALIDATED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()
