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

from planpilot.approval import ApprovalRequiredError
from planpilot.authority import (LEGACY_PUBLISH_KEY_PREFIX,
                                 AuthorityConflictError, RuntimeAuthority,
                                 _legacy_publish_key)
from planpilot.clock import FixedClock
from planpilot.persistence import Database
from planpilot.security import issue_token
from planpilot.store import (DigestMismatchError, SchemaViolationError,
                             VersionConflictError, canonical_plan_digest)

NOW = "2026-09-14T08:00:00+08:00"
HORIZON_END = "2026-09-18T17:00:00+08:00"
ROOT = Path(__file__).resolve().parents[2]


def clock(now: str = NOW) -> FixedClock:
    """Server-owned fixed clock; the fixture horizon ends 2026-09-18 17:00."""
    return FixedClock(now)


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
    authority = RuntimeAuthority(db, clock())
    content = signed_content(fixtures)
    stored = install(authority, content)
    assert stored["lifecycle"]["status"] == "PROPOSED"
    request = authority.request_approval(
        content["plan_id"], 1, content["plan_digest"],
        "publish_plan", "planner",
    )
    approval = request["approvals"][0]
    assert authority.required_permission(approval["approval_request_id"]) == "approve_publish"
    with pytest.raises(ApprovalRequiredError):
        authority.publish_plan(
            content["plan_id"], 1, content["plan_digest"],
            request["approval_set_id"], "planner", "planner",
        )
    decided = authority.decide_approval(
        approval["approval_request_id"], "APPROVED", "planner", "planner",
    )
    assert decided["aggregate_status"] == "APPROVED"
    published = authority.publish_plan(
        content["plan_id"], 1, content["plan_digest"],
        request["approval_set_id"], "planner", "planner",
    )
    assert published["status"] == "PUBLISHED" and published["published_version"] == 1
    revision = authority.revision
    assert authority.publish_plan(
        content["plan_id"], 1, content["plan_digest"],
        request["approval_set_id"], "planner", "planner",
    ) == published
    assert authority.revision == revision
    db.close()

    restored_db = Database(path)
    restored = RuntimeAuthority(restored_db, clock("2026-09-14T09:00:00+08:00"))
    assert restored.revision == revision
    assert restored.get_plan(content["plan_id"], 1)["lifecycle"]["status"] == "PUBLISHED"
    assert restored_db.verify_audit()
    restored_db.close()


def test_invalid_plan_and_unverified_evidence_fail_without_state_change(tmp_path, fixtures):
    db = Database(tmp_path / "state.db")
    authority = RuntimeAuthority(db, clock())
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
    authority = RuntimeAuthority(db, clock())
    first = signed_content(fixtures)
    install(authority, first)
    old = authority.request_approval(
        first["plan_id"], 1, first["plan_digest"], "publish_plan", "p"
    )
    second = signed_content(fixtures, version=2)
    install(authority, second)
    assert authority.get_plan(first["plan_id"], 1)["lifecycle"]["status"] == "SUPERSEDED"
    # p2-5: publish_plan delegates to the §4.5 publisher, whose pinned
    # step order (design §4) version-preflights BEFORE the approval-set
    # load — the stale v1 binding is blocked with PLAN_VERSION_CONFLICT.
    # The invalidated-set leg stays covered by the publisher matrix
    # (§8 case 12, test_publisher_transaction) and the HTTP route test
    # in test_p1_3_web_approval (v2, never superseded).
    with pytest.raises(VersionConflictError):
        authority.publish_plan(
            first["plan_id"], 1, first["plan_digest"], old["approval_set_id"],
            "p", "planner",
        )
    assert authority.get_plan(first["plan_id"], 1)["lifecycle"]["status"] == "SUPERSEDED"
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
    server = api.Server(("127.0.0.1", 0), db, secret, tmp_path, clock=clock())
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


# ---------------------------------------------------------------- p2-5 reviewer P1
#
# The legacy positional publish_plan entry must derive an idempotency key
# that satisfies the contract (16–128 chars) for ANY plan_id, because
# PublisherService.publish (the non-HTTP convenience path) does not
# re-run the input schema and the key lands in idempotency_registry
# verbatim. A raw f"{plan_id}:{version}" key was contract-illegal for a
# 1-char plan_id (length 3). The fix: fixed-length hash of the canonical
# binding — deterministic (same binding replays, case A) yet bounded.

def test_legacy_publish_key_contract_bounds_and_determinism():
    short = _legacy_publish_key("P", 1)          # 1-char plan id
    long = _legacy_publish_key("X" * 5000, 1)    # absurdly long plan id
    for key in (short, long, _legacy_publish_key("PLAN-RUNTIME-1", 1)):
        assert key.startswith(LEGACY_PUBLISH_KEY_PREFIX)
        assert 16 <= len(key) <= 128
    # Same binding → same key (replay stays possible)…
    assert _legacy_publish_key("P", 1) == short
    assert _legacy_publish_key("X" * 5000, 1) == long
    # …different version → different key, and no separator injection:
    # plan_id "A:1" version 2 must NOT collide with plan_id "A"
    # version "1:2"-style raw concat would.
    assert _legacy_publish_key("PLAN-A", 1) != _legacy_publish_key("PLAN-A", 2)
    assert _legacy_publish_key("A:1", 2) != _legacy_publish_key("A", 1)


def test_legacy_delegation_stores_contract_valid_key_and_replays_receipt(tmp_path, fixtures):
    db = Database(tmp_path / "state.db")
    authority = RuntimeAuthority(db, clock())
    content = signed_content(fixtures)
    install(authority, content)
    request = authority.request_approval(
        content["plan_id"], 1, content["plan_digest"],
        "publish_plan", "planner",
    )
    authority.decide_approval(
        request["approvals"][0]["approval_request_id"], "APPROVED",
        "planner", "planner",
    )
    first = authority.publish_plan(
        content["plan_id"], 1, content["plan_digest"],
        request["approval_set_id"], "planner", "planner",
    )
    with db.lock:
        row = db.conn.execute(
            "SELECT idempotency_key FROM idempotency_registry"
        ).fetchone()
    assert row is not None
    stored_key = row["idempotency_key"]
    assert 16 <= len(stored_key) <= 128
    assert stored_key == _legacy_publish_key(content["plan_id"], 1)
    # Replay of the same binding returns the original receipt verbatim.
    again = authority.publish_plan(
        content["plan_id"], 1, content["plan_digest"],
        request["approval_set_id"], "planner", "planner",
    )
    assert again == first
    db.close()
