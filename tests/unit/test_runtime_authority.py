"""P0-2: the runtime uses PlanStore + ApprovalService as its only authority."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from planpilot.approval import ApprovalRequiredError, ApprovalSetInvalidatedError
from planpilot.authority import AuthorityConflictError, RuntimeAuthority
from planpilot.persistence import Database
from planpilot.security import issue_token
from planpilot.store import DigestMismatchError, SchemaViolationError, canonical_plan_digest

NOW = "2026-09-14T08:00:00+08:00"
HORIZON_END = "2026-09-18T17:00:00+08:00"
ROOT = Path(__file__).resolve().parents[2]


def signed_content(fixtures, *, version=1, plan_id="PLAN-RUNTIME-1"):
    kpis = fixtures.make_kpis(overtime_hours=0, secondary_skill_assignment_count=0)
    draft = fixtures.make_content(
        plan_id=plan_id, plan_version=version, kpis=kpis,
        consequential_approval_required=False,
    )
    digest = canonical_plan_digest(draft)
    return fixtures.make_content(
        plan_id=plan_id, plan_version=version, kpis=kpis, digest=digest,
        consequential_approval_required=False,
    )


def validator_result(content):
    return {
        "plan_id": content["plan_id"],
        "plan_version": content["plan_version"],
        "plan_digest": content["plan_digest"],
        "recomputed_plan_digest": content["plan_digest"],
        "digest_verified": True,
        "is_feasible": True,
        "infeasible_reason": None,
        "hard_violations": [],
        "kpis": copy.deepcopy(content["kpis"]),
        "approval_requirements": [{
            "action": "publish_plan",
            "approver_role": "Production Planner",
            "reason": "Server-derived publication confirmation.",
        }],
        "quarantine_impact": [],
    }


def impacts():
    return {"publish_plan": {
        "affected_order_ids": ["ORD-A"],
        "changed_operation_count": 1,
        "kpi_deltas": {},
        "reason_codes": ["PUBLISH_CONFIRMATION_REQUIRED"],
    }}


def install(authority, content):
    return authority.install_validated_plan(
        content, validator_result(content), impacts(), HORIZON_END, NOW,
        actor="validator",
    )


def test_database_has_no_parallel_plan_or_approval_business_api(tmp_path):
    db = Database(tmp_path / "state.db")
    try:
        for method in (
            "save_plan", "get_plan", "request_approval", "decide_approval",
            "approval_permission", "publish_plan",
        ):
            assert not hasattr(db, method)
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert not {"plans", "revisions", "bound_approvals", "publications"} & tables
    finally:
        db.close()


def test_authoritative_round_trip_approval_and_publish(tmp_path, fixtures):
    path = tmp_path / "state.db"
    db = Database(path)
    authority = RuntimeAuthority(db)
    content = signed_content(fixtures)
    stored = install(authority, content)
    assert stored["lifecycle"]["status"] == "PROPOSED"
    request = authority.request_approval(
        content["plan_id"], 1, content["plan_digest"],
        "publish_plan", "planner", NOW,
    )
    approval = request["approvals"][0]
    assert authority.required_permission(approval["approval_request_id"]) == "approve_publish"
    with pytest.raises(ApprovalRequiredError):
        authority.publish_plan(
            content["plan_id"], 1, content["plan_digest"],
            request["approval_set_id"], "planner", "planner", NOW,
        )
    decided = authority.decide_approval(
        approval["approval_request_id"], "APPROVED", "planner", "planner",
        "2026-09-14T08:01:00+08:00",
    )
    assert decided["aggregate_status"] == "APPROVED"
    published = authority.publish_plan(
        content["plan_id"], 1, content["plan_digest"],
        request["approval_set_id"], "planner", "planner",
        "2026-09-14T08:02:00+08:00",
    )
    assert published["status"] == "PUBLISHED" and published["published_version"] == 1
    revision = authority.revision
    assert authority.publish_plan(
        content["plan_id"], 1, content["plan_digest"],
        request["approval_set_id"], "planner", "planner",
        "2026-09-14T08:03:00+08:00",
    ) == published
    assert authority.revision == revision
    db.close()

    restored_db = Database(path)
    restored = RuntimeAuthority(restored_db)
    assert restored.revision == revision
    assert restored.get_plan(content["plan_id"], 1)["lifecycle"]["status"] == "PUBLISHED"
    assert restored_db.verify_audit()
    restored_db.close()


def test_invalid_plan_and_unverified_evidence_fail_without_state_change(tmp_path, fixtures):
    db = Database(tmp_path / "state.db")
    authority = RuntimeAuthority(db)
    content = signed_content(fixtures)
    invalid = {**content, "unexpected": True}
    with pytest.raises(SchemaViolationError):
        install(authority, invalid)
    unverified = validator_result(content)
    unverified["digest_verified"] = False
    with pytest.raises(ValueError, match="independently verify"):
        authority.install_validated_plan(
            content, unverified, impacts(), HORIZON_END, NOW
        )
    assert authority.revision == 0
    assert authority.get_plan(content["plan_id"]) is None
    db.close()


def test_regeneration_invalidates_old_approval_and_blocks_stale_publish(tmp_path, fixtures):
    db = Database(tmp_path / "state.db")
    authority = RuntimeAuthority(db)
    first = signed_content(fixtures)
    install(authority, first)
    old = authority.request_approval(
        first["plan_id"], 1, first["plan_digest"], "publish_plan", "p", NOW
    )
    second = signed_content(fixtures, version=2)
    install(authority, second)
    assert authority.get_plan(first["plan_id"], 1)["lifecycle"]["status"] == "SUPERSEDED"
    with pytest.raises(ApprovalSetInvalidatedError):
        authority.publish_plan(
            first["plan_id"], 1, first["plan_digest"], old["approval_set_id"],
            "p", "planner", "2026-09-14T08:02:00+08:00",
        )
    assert authority.get_plan(first["plan_id"], 2)["lifecycle"]["status"] == "PROPOSED"
    db.close()


def test_sqlite_backup_restores_authority_snapshot(tmp_path, fixtures):
    from planpilot.backup import backup_database
    source, target = tmp_path / "source.db", tmp_path / "backup.db"
    db = Database(source)
    content = signed_content(fixtures)
    install(RuntimeAuthority(db), content)
    db.close()
    backup_database(source, target)
    restored_db = Database(target)
    restored = RuntimeAuthority(restored_db)
    assert restored.get_plan(content["plan_id"], 1)["content"] == content
    restored_db.close()


def test_stale_bridge_cannot_overwrite_a_newer_snapshot(tmp_path, fixtures):
    path = tmp_path / "state.db"
    first_db, second_db = Database(path), Database(path)
    first, stale = RuntimeAuthority(first_db), RuntimeAuthority(second_db)
    content = signed_content(fixtures)
    install(first, content)
    with pytest.raises(AuthorityConflictError, match="snapshot changed"):
        install(stale, signed_content(fixtures, plan_id="PLAN-STALE"))
    restored = RuntimeAuthority(second_db)
    assert restored.get_plan(content["plan_id"], 1)["content"] == content
    assert restored.get_plan("PLAN-STALE") is None
    first_db.close()
    second_db.close()


def test_tampered_sqlite_snapshot_is_rejected_on_restart(tmp_path, fixtures):
    path = tmp_path / "state.db"
    db = Database(path)
    content = signed_content(fixtures)
    install(RuntimeAuthority(db), content)
    row = db.conn.execute(
        "SELECT plan_store_json FROM authority_state WHERE singleton=1"
    ).fetchone()
    state = __import__("json").loads(row[0])
    state["content"][0]["kpis"]["on_time_orders"] = 999
    db.conn.execute(
        "UPDATE authority_state SET plan_store_json=? WHERE singleton=1",
        (__import__("json").dumps(state),),
    )
    with pytest.raises(DigestMismatchError):
        RuntimeAuthority(db)
    db.close()


def test_http_approval_and_publish_use_runtime_authority(tmp_path, fixtures):
    spec = importlib.util.spec_from_file_location("authority_api", ROOT / "tools/api_server.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    db = Database(tmp_path / "state.db")
    secret = "authority-test-secret-32-characters"
    server = api.Server(("127.0.0.1", 0), db, secret, tmp_path)
    content = signed_content(fixtures)
    install(server.authority, content)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    token = issue_token("planner", "planner", secret)

    def post(path, body):
        request = Request(
            base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    try:
        starting_revision = server.authority.revision
        with pytest.raises(HTTPError) as invalid:
            post("/approval/request", {
                "plan_id": content["plan_id"],
                "plan_version": 1,
                "plan_digest": content["plan_digest"],
                "action": "publish_plan",
                "caller_owned_requirement": "forbidden",
            })
        assert invalid.value.code == 400
        assert server.authority.revision == starting_revision

        binding = {
            "plan_id": content["plan_id"], "plan_version": 1,
            "plan_digest": content["plan_digest"],
        }
        approval_set = post("/approval/request", {**binding, "action": "publish_plan"})
        request_id = approval_set["approvals"][0]["approval_request_id"]
        decided = post("/approval/decide", {"request_id": request_id, "decision": "APPROVED"})
        assert decided["aggregate_status"] == "APPROVED"
        published = post("/publish", {
            "plan_id": binding["plan_id"],
            "expected_plan_version": binding["plan_version"],
            "plan_digest": binding["plan_digest"],
            "approval_set_id": approval_set["approval_set_id"],
            "idempotency_key": "publish-plan-http-test-0001",
        })
        assert published["status"] == "PUBLISHED"
        with urlopen(Request(
            base + "/plans?plan_id=" + content["plan_id"],
            headers={"Authorization": "Bearer " + token},
        ), timeout=10) as response:
            loaded = json.load(response)
        assert loaded["content"] == content
        assert loaded["lifecycle"]["status"] == "PUBLISHED"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        db.close()
