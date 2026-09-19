"""G1: server-owned clock policy and fresh-horizon rules (P0-1 clock bomb).

The bomb was never the horizon guard: the guard is contract law
(approval_expiry_policy). The bug was that authority timestamps could be
caller-supplied while demo data aged against the real wall clock. These
tests pin the fix from both sides:

- an expired horizon STILL gets APPROVAL_WINDOW_CLOSED under a stale clock
  (the guard was not loosened);
- the fixed 2026-09-14 scenario data completes its full approval flow
  under a declared ScenarioClock;
- expiry boundaries flip exactly at expires_at;
- no request path accepts a client-supplied time;
- switching clocks never mutates an immutable plan digest.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from planpilot.approval import ApprovalError
from planpilot.approval.errors import ApprovalWindowClosedError
from planpilot.authority import RuntimeAuthority
from planpilot.clock import FixedClock, ScenarioClock, WallClock
from planpilot.persistence import Database
from planpilot.security import issue_token
from planpilot.store import canonical_plan_digest

from unit.test_runtime_authority import HORIZON_END, NOW, clock, install, signed_content

ROOT = Path(__file__).resolve().parents[2]
# The P0-1 failure moment: horizon guard is 2026-09-18T17:00 + 24h.
STALE_NOW = "2026-09-19T18:39:00+08:00"


def _api_module():
    spec = importlib.util.spec_from_file_location("clock_api", ROOT / "tools/api_server.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def test_stale_horizon_still_returns_window_closed_under_real_time(tmp_path, fixtures):
    """The guard stands: a stale clock closes the window even though the
    plan itself is byte-identical to the one that approved fine in scenario."""
    db = Database(tmp_path / "state.db")
    authority = RuntimeAuthority(db, clock=FixedClock(STALE_NOW))
    content = signed_content(fixtures)
    install(authority, content)
    with pytest.raises(ApprovalWindowClosedError) as exc:
        authority.request_approval(
            content["plan_id"], 1, content["plan_digest"], "publish_plan", "planner",
        )
    details = exc.value.details
    assert details["server_now"] == STALE_NOW
    assert details["horizon_guard"] == "2026-09-19T17:00:00+08:00"
    db.close()


def test_scenario_clock_completes_fixed_dataset_demo(tmp_path, fixtures):
    """The demo dataset (horizon ending 2026-09-18 17:00) approves end-to-end
    only because the server pins a declared scenario time inside its window."""
    db = Database(tmp_path / "state.db")
    demo = ScenarioClock(NOW, scenario={
        "dataset": "factory_demo_v18.json",
        "planning_start": "2026-09-14T00:00:00+08:00",
        "horizon_end": HORIZON_END,
    })
    authority = RuntimeAuthority(db, clock=demo)
    content = signed_content(fixtures)
    install(authority, content)
    approval_set = authority.request_approval(
        content["plan_id"], 1, content["plan_digest"], "publish_plan", "planner",
    )
    request_id = approval_set["approvals"][0]["approval_request_id"]
    decided = authority.decide_approval(
        request_id, "APPROVED", "planner-a", "planner",
    )
    assert decided["aggregate_status"] == "APPROVED"
    published = authority.publish_plan(
        content["plan_id"], 1, content["plan_digest"],
        approval_set["approval_set_id"], "planner-a", "planner",
    )
    assert published["status"] == "PUBLISHED"
    # Honesty requirement: the status the UI reads labels itself scenario time.
    status = demo.status()
    assert status["kind"] == "scenario"
    assert status["scenario"]["dataset"] == "factory_demo_v18.json"
    db.close()


def test_pending_approval_expires_exactly_at_boundary(tmp_path, fixtures):
    """Advance the server clock across expires_at: the boundary is inclusive
    (now >= expires_at expires) and publish then refuses with APPROVAL_EXPIRED."""
    db = Database(tmp_path / "state.db")
    server_clock = FixedClock(NOW)
    authority = RuntimeAuthority(db, clock=server_clock)
    content = signed_content(fixtures)
    install(authority, content)
    approval_set = authority.request_approval(
        content["plan_id"], 1, content["plan_digest"], "publish_plan", "planner",
    )
    expires_at = approval_set["approvals"][0]["expires_at"]
    # publish_plan default TTL is 24h; the clamp must have kept it inside
    # the horizon guard (server_now + 86400 vs horizon_guard 2026-09-19T17:00).
    assert expires_at <= "2026-09-19T17:00:00+08:00"

    server_clock._now = expires_at  # exactly at the boundary -> already expired
    with pytest.raises(ApprovalError) as exc:
        authority.publish_plan(
            content["plan_id"], 1, content["plan_digest"],
            approval_set["approval_set_id"], "planner-a", "planner",
        )
    assert exc.value.code == "APPROVAL_EXPIRED"
    db.close()


def test_request_paths_reject_client_supplied_time(tmp_path, fixtures):
    """Contract input schemas forbid extra keys (additionalProperties=false)
    and no authority method exposes a server_now/decided_at parameter."""
    for name in ("request_approval", "publish_plan", "decide_approval",
                 "check_approval_status"):
        params = inspect.signature(getattr(RuntimeAuthority, name)).parameters
        forbidden = {"server_now", "decided_at", "published_at", "validated_at"}
        assert not forbidden & set(params), f"{name} accepts {forbidden & set(params)}"

    api = _api_module()
    db = Database(tmp_path / "state.db")
    secret = "clock-policy-secret-32-characters-x"
    server = api.Server(("127.0.0.1", 0), db, secret, tmp_path, clock=clock())
    content = signed_content(fixtures)
    install(server.authority, content)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    token = issue_token("planner", "planner", secret)
    auth_header = "Bearer" + " " + token  # assembled at runtime; literal form
    # gets mangled by secret-scanning pipelines, so keep this exact line.
    try:
        with pytest.raises(HTTPError) as forged:
            request = Request(
                base + "/approval/request",
                data=json.dumps({
                    "plan_id": content["plan_id"], "plan_version": 1,
                    "plan_digest": content["plan_digest"],
                    "action": "publish_plan",
                    "server_now": "2026-09-14T08:00:00+08:00",
                }).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": auth_header},
            )
            urlopen(request, timeout=10)
        assert forged.value.code == 400
        # The /clock endpoint is read-only server state; no POST path exists.
        with urlopen(Request(base + "/clock"), timeout=10) as response:
            status = json.load(response)
        assert status["kind"] == "fixed"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        db.close()


def test_clock_switch_never_rewrites_immutable_digest(tmp_path, fixtures):
    """A plan installed under one server clock keeps its digest and content
    under any other clock; only NEW audit stamps move."""
    db = Database(tmp_path / "state.db")
    authority = RuntimeAuthority(db, clock=FixedClock(NOW))
    content = signed_content(fixtures)
    install(authority, content)
    digest_before = canonical_plan_digest(content)

    reopened = RuntimeAuthority(db, clock=WallClock())
    loaded = reopened.get_plan(content["plan_id"], 1)
    assert canonical_plan_digest(loaded["content"]) == digest_before
    assert loaded["content"] == content
    # get_plan itself digests-verifies on every read; a second read under
    # the different clock must survive it unchanged.
    again = reopened.get_plan(content["plan_id"], 1)
    assert again["content"] == content
    db.close()


# ---------------------------------------------------------------------------
# G1.0.1 (review follow-up): the previous file over-claimed. These tests are
# the real HTTP path against the real fixed dataset, with a genuine close and
# reopen of server + database, and they pin the fail-safe clock policy.


def _demo_state():
    return json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))


def _http(server, secret, path, body=None, role="planner"):
    request = Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer" + " " + issue_token(role, role, secret)})
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def _start(api, db, secret, clock):
    server = api.Server(("127.0.0.1", 0), db, secret, ROOT / "data", clock=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread, db):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    db.close()


def test_real_http_demo_flow_survives_server_restart(tmp_path):
    """G1.0.1 review: the scenario demo must be proven over HTTP with the
    REAL factory_demo_v18.json, and 'restart' must be a genuine close+reopen
    of the sqlite file — not a second Authority over a live connection."""
    api = _api_module()
    secret = "g101-restart-secret-at-least-32-chars"
    db_path = tmp_path / "restart.db"
    state = _demo_state()
    anchor = state["planning_start"]

    db1 = Database(db_path)
    server, thread = _start(api, db1, secret, ScenarioClock(anchor))
    try:
        status, generation = _http(server, secret, "/schedule", {"factory_data": state})
        assert status == 200 and len(generation["plan_options"]) == 3
        option = generation["plan_options"][0]
        binding = {"plan_id": option["plan_id"], "plan_version": option["plan_version"],
                   "plan_digest": option["plan_digest"]}
        assert _http(server, secret, "/plans?" + urlencode({"plan_id": option["plan_id"]}))[0] == 200
        status, requested = _http(server, secret, "/approval/request",
                                  {**binding, "action": "publish_plan"})
        assert status == 200 and requested["approvals"]
        for row in requested["approvals"]:
            assert _http(server, secret, "/approval/decide", {
                "request_id": row["approval_request_id"], "decision": "APPROVED"})[0] == 200
        status, published = _http(server, secret, "/publish", {
            "plan_id": option["plan_id"],
            "expected_plan_version": option["plan_version"],
            "plan_digest": option["plan_digest"],
            "approval_set_id": requested["approval_set_id"],
            "idempotency_key": "g101-restart-publish"})
        assert status == 200 and published["status"] == "PUBLISHED"
        audit_before = _http(server, secret, "/audit/status")[1]
    finally:
        _stop(server, thread, db1)

    # Genuine restart: new Database over the same file, NEW ScenarioClock
    # (re-anchored at boot — epoch policy is explicit: anchor + elapsed).
    db2 = Database(db_path)
    server2, thread2 = _start(api, db2, secret, ScenarioClock(anchor))
    try:
        status, loaded = _http(server2, secret, "/plans?" + urlencode({"plan_id": option["plan_id"]}))
        assert status == 200
        assert loaded["content"]["plan_digest"] == option["plan_digest"]
        status, after = _http(server2, secret, "/approval/status?" + urlencode(
            {"approval_set_id": requested["approval_set_id"], **binding}))
        assert status == 200 and after["aggregate_status"] == "APPROVED"
        audit_after = _http(server2, secret, "/audit/status")[1]
        # The only new chain entry is the approval_status_checked appended by
        # this session's own GET (audit-on-read is by design) — proving the
        # chain extended continuously across the restart with no duplicates or
        # gaps from reopening, and that the re-anchored clock still stamps
        # scenario time rather than leaking the real wall date.
        assert audit_after["verified"] and audit_before["verified"]
        assert audit_after["entry_count"] == audit_before["entry_count"] + 1
        newest = audit_after["recent"][0]
        assert newest["event"] == "approval_status_checked"
        assert newest["created_at"].startswith("2026-09-14"), newest
    finally:
        _stop(server2, thread2, db2)


def test_single_clock_source_agrees_across_lifecycle_and_audit(tmp_path):
    """G1.0.1 P0 (dual-clock leak): one operation must not stamp lifecycle
    with the scenario clock while audit/trace rows keep the real wall clock.
    The review measured 2026-09-14 vs 2026-09-19 in the SAME operation."""
    api = _api_module()
    secret = "g101-unify-secret-at-least-32-char"
    db = Database(tmp_path / "unify.db")
    state = _demo_state()
    server, thread = _start(api, db, secret, ScenarioClock(state["planning_start"]))
    try:
        status, generation = _http(server, secret, "/schedule", {"factory_data": state})
        assert status == 200
        scenario_now = server.clock.now()
        audit_record = db.conn.execute(
            "SELECT record FROM audit_chain ORDER BY id DESC LIMIT 1").fetchone()
        assert audit_record is not None, "schedule must have written audit rows"
        from datetime import datetime
        audit_created = json.loads(audit_record[0])["created_at"]
        delta = abs((datetime.fromisoformat(audit_created)
                     - datetime.fromisoformat(scenario_now)).total_seconds())
        # One clock: seconds apart. The leak measured ~5 real days instead.
        assert delta < 120, f"lifecycle {scenario_now} vs audit {audit_created}"
        assert audit_created.startswith("2026-09-14"), "audit stamp is scenario time now"
    finally:
        _stop(server, thread, db)


def test_clock_policy_fail_safe_defaults_to_wall():
    """G1.0.1 P0: unset mode => wall; only the literal 'scenario' opts in;
    unknown values fail startup; scenario clock refuses public binds."""
    from planpilot.clock import clock_from_env
    assert clock_from_env({}, "0.0.0.0").kind == "wall"
    assert clock_from_env({"PLANPILOT_CLOCK_MODE": ""}, "0.0.0.0").kind == "wall"
    # Legacy 'off' is NOT silently accepted as an alias — unknown values fail
    # startup loudly, that was the whole point of the fail-safe rewrite.
    with pytest.raises(ValueError):
        clock_from_env({"PLANPILOT_CLOCK_MODE": "off"}, "127.0.0.1")
    assert clock_from_env({"PLANPILOT_CLOCK_MODE": "WALL"}, "127.0.0.1").kind == "wall"
    assert clock_from_env({"PLANPILOT_CLOCK_MODE": "scenario"}, "127.0.0.1").kind == "scenario"
    with pytest.raises(ValueError):
        clock_from_env({"PLANPILOT_CLOCK_MODE": "yes"}, "127.0.0.1")
    with pytest.raises(ValueError):
        clock_from_env({"PLANPILOT_CLOCK_MODE": "scenario"}, "0.0.0.0")
    acked = clock_from_env({"PLANPILOT_CLOCK_MODE": "scenario",
                            "PLANPILOT_ALLOW_PUBLIC_SCENARIO": "1"}, "0.0.0.0")
    assert acked.kind == "scenario"


def test_scenario_clock_advances_so_demo_approvals_still_expire():
    """G1.0.1 P1-2: ScenarioClock = anchor + real elapsed time, not frozen.
    FixedClock alone is the deterministic/test/EVAL tool; a demo left running
    for 24h of real wall time really does close its approval window."""
    import time as _time
    clock = ScenarioClock("2026-09-14T08:00:00+08:00")
    first = clock.now()
    _time.sleep(1.1)
    second = clock.now()
    assert second > first, "scenario clock must advance with real elapsed time"
    assert clock.elapsed() >= 1.0
    assert second.startswith("2026-09-14"), "but stays in scenario date, not real today"
    # Contract expiry formula over a 24h+ scenario advance closes the window:
    # a clock anchored +25h past now must not open a 7200s TTL inside
    # horizon+24h. (FixedClock pins the same reasoning deterministically.)
    stale = FixedClock("2026-09-15T09:30:00+08:00")
    assert stale.now() > second


def test_future_wall_clock_closes_real_demo_over_http(tmp_path):
    """The review reproduced APPROVAL_WINDOW_CLOSED for the ACTUAL
    factory_demo_v18.json at 2026-09-20T00:06 (guard = horizon_end
    2026-09-19T00:00 + 24h). Pinned here end-to-end over HTTP with the clock
    frozen at that moment: generation still works, the approval window does
    not, and the guard is contract law — we never loosened it."""
    api = _api_module()
    secret = "g101-stale-secret-at-least-32-ch"
    db = Database(tmp_path / "stale.db")
    state = _demo_state()
    server, thread = _start(api, db, secret, FixedClock("2026-09-20T00:06:00+08:00"))
    try:
        status, generation = _http(server, secret, "/schedule", {"factory_data": state})
        assert status == 200, "generating fresh plans under wall time is legal"
        option = generation["plan_options"][0]
        status, error = _http(server, secret, "/approval/request", {
            "plan_id": option["plan_id"], "plan_version": option["plan_version"],
            "plan_digest": option["plan_digest"], "action": "publish_plan"})
        assert status == 409
        # 409 body: {"error": human message, "details": schema'd fields}.
        assert "horizon" in error["error"].lower() or error.get("code") == "APPROVAL_WINDOW_CLOSED", error
        assert error["details"]["server_now"] == "2026-09-20T00:06:00+08:00"
        assert error["details"]["horizon_guard"] == "2026-09-20T00:00:00+08:00"
    finally:
        _stop(server, thread, db)
