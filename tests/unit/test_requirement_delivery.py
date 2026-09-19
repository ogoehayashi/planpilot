"""Regression evidence for the compact production adapter and HTTP boundary."""
from copy import deepcopy
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from planpilot.agent.bedrock import BedrockIntentClient
from planpilot.backup import backup_database
from planpilot.domain.importer import factory_from_dict, load_factory
from planpilot.domain.planning import solve, validate_plan, reserve_materials
from planpilot.clock import ScenarioClock
from planpilot.persistence import Database
from planpilot.security import issue_token, authenticate

ROOT = Path(__file__).resolve().parents[2]


def raw_factory():
    return {"horizon": 100, "orders": [
        {"order_id": "o1", "product_id": "p1", "quantity": 1, "due_at": 25,
         "operations": [{"machine_id": "m1", "worker_id": "w1", "duration": 5, "material_id": "steel", "material_qty": 8},
                        {"machine_id": "m2", "worker_id": "w1", "duration": 4}]}],
        "workers": [{"worker_id": "w1"}],
        "inventory": {"steel": {"batches": [{"batch_id": "old", "quantity": 5, "available_at": 0},
                                               {"batch_id": "new", "quantity": 5, "available_at": 20}]}}}


def test_fifo_is_pure_and_waits_for_last_allocated_batch():
    factory = factory_from_dict(raw_factory())
    before = deepcopy(factory.inventory)
    first = solve(factory, "Balanced")
    assert factory.inventory == before
    assert first.operations[0]["start"] >= 20
    assert [a["batch_id"] for a in first.material_reservations["o1"]["allocations"]] == ["old", "new"]
    assert asdict(first) == asdict(solve(factory, "Balanced"))
    invalid = deepcopy(first.operations)
    invalid[0]["start"], invalid[0]["end"] = 0, 5
    assert any(v["type"] == "material_availability" for v in validate_plan(invalid, factory))


def test_shortage_rolls_back_all_lines_and_is_reported():
    raw = raw_factory()
    raw["orders"][0]["operations"][1].update(material_id="missing", material_qty=1)
    second = deepcopy(raw["orders"][0])
    second.update(order_id="o2", due_at=30)
    second["operations"] = second["operations"][:1]
    raw["orders"].append(second)
    factory = factory_from_dict(raw)
    allocations = reserve_materials(factory)
    assert allocations["o1"]["allocations"] == []
    assert allocations["o2"]["status"] == "READY"
    plan = solve(factory, "Balanced")
    assert len(plan.unscheduled_operations) == 2
    assert plan.kpis["eligible_order_coverage_rate"] == 0.5
    assert plan.kpis["on_time_rate"] == 0.5
    assert not plan.violations


def test_calendars_maintenance_and_precedence_are_enforced():
    raw = raw_factory()
    raw["shifts"] = [{"machine_id": "m1", "start": 20, "end": 40},
                     {"machine_id": "m2", "start": 20, "end": 50},
                     {"worker_id": "w1", "start": 22, "end": 45}]
    raw["maintenance"] = [{"machine_id": "m1", "start": 22, "end": 30},
                          {"worker_id": "w1", "start": 35, "end": 38}]
    plan = solve(factory_from_dict(raw), "Balanced")
    assert not plan.violations and len(plan.operations) == 2
    assert plan.operations[0]["start"] == 30
    assert plan.operations[1]["start"] >= 38
    raw["shifts"][2]["end"] = 34
    impossible = solve(factory_from_dict(raw), "Balanced")
    assert not impossible.operations and len(impossible.unscheduled_operations) == 2
    assert impossible.kpis["on_time_rate"] == 0


def test_setup_interval_cannot_overlap_maintenance():
    raw = {"horizon": 50, "orders": [
        {"order_id": "a", "product_id": "p1", "quantity": 1, "due_at": 5,
         "operations": [{"machine_id": "m", "duration": 5}]},
        {"order_id": "b", "product_id": "p2", "quantity": 1, "due_at": 40,
         "operations": [{"machine_id": "m", "duration": 5, "release": 15, "changeover": 5}]}],
        "maintenance": [{"machine_id": "m", "start": 8, "end": 13}]}
    factory = factory_from_dict(raw)
    plan = solve(factory, "Balanced")
    assert not plan.violations
    b = next(r for r in plan.operations if r["order_id"] == "b")
    assert b["start"] >= 18


@pytest.mark.parametrize("token", ["", "nonsense", "x.y", "a" * 5000])
def test_malformed_authentication_fails_closed(token):
    with pytest.raises(PermissionError):
        authenticate(token, "secret", "plan")


@pytest.mark.parametrize("value", [{"request": "plan", "kpis": {}},
                                  {"request": "plan", "constraints": {"approved": True}},
                                  {"request": "plan", "constraints": {"profile": "invented"}}])
def test_bedrock_rejects_nested_authority_fields(value):
    with pytest.raises(ValueError):
        BedrockIntentClient.validate_output(value)


def test_excel_preserves_batches_and_routing_order(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    content = {
        "Orders": [["order_id", "product_id", "quantity", "due_at"], ["o", "p", 1, 50]],
        "Operations": [["order_id", "operation_no", "machine_id", "duration", "material_id", "material_qty"],
                       ["o", 2, "m", 2, None, None], ["o", 1, "m", 3, "steel", 8]],
        "Inventory": [["material_id", "batch_id", "quantity", "available_at"], ["steel", "old", 5, 0], ["steel", "new", 5, 20]]}
    for name, rows in content.items():
        sheet = wb.create_sheet(name)
        for row in rows:
            sheet.append(row)
    path = tmp_path / "factory.xlsx"
    wb.save(path)
    wb.close()
    factory = load_factory(path)
    assert [o.duration_minutes for o in factory.orders[0].operations] == [3, 2]
    assert len(factory.inventory["steel"]["batches"]) == 2
    assert solve(factory, "Balanced").operations[0]["start"] >= 20


def test_http_authentication_and_full_publish_flow(tmp_path):
    spec = importlib.util.spec_from_file_location("planning_api", ROOT / "tools/api_server.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    db = Database(tmp_path / "state.db")
    secret = "test-secret-that-is-at-least-32-characters"
    # G1.0.1: publish flow on the real fixed dataset needs the scenario clock
    # or it reddens once the wall clock passes horizon_end + 24h (2026-09-20).
    demo = json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))
    server = api.Server(("127.0.0.1", 0), db, secret, tmp_path,
                        clock=ScenarioClock(demo["planning_start"]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(path, body=None, role="planner"):
        token = issue_token(role, role, secret) if role else ""
        req = Request(f"http://127.0.0.1:{server.server_port}" + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)
    try:
        assert call("/health")[0] == 200
        assert call("/schedule", {"factory_data": raw_factory()}, role=None)[0] == 403
        assert call("/schedule", {"factory_file": "../outside.json"})[0] == 403
        state = json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))
        status, result = call("/schedule", {"factory_data": state})
        assert status == 200
        assert result["stored_plan_count"] == 3
        assert result["state"] == "PLANS_VALIDATED"
        assert db.conn.execute("SELECT revision FROM authority_state").fetchone()[0] == 3
        binding = {
            "plan_id": result["plan_id"],
            "plan_version": result["version"],
            "plan_digest": result["plan_digest"],
            "action": "publish_plan",
        }
        approval = call("/approval/request", binding)[1]
        aggregate = None
        for request in approval["approvals"]:
            aggregate = call("/approval/decide", {"request_id": request["approval_request_id"], "decision": "APPROVED"})[1]["aggregate_status"]
        assert aggregate == "APPROVED"
        publish = {
            "plan_id": result["plan_id"],
            "expected_plan_version": result["version"],
            "plan_digest": result["plan_digest"],
            "approval_set_id": approval["approval_set_id"],
            "idempotency_key": "runtime-v18-publish-test-0001",
        }
        assert call("/publish", publish)[1]["status"] == "PUBLISHED"
        assert call("/metrics")[1]["schedule"]["count"] >= 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()
