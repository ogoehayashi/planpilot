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
