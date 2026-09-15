"""Regression evidence for the compact production adapter and HTTP boundary."""
from concurrent.futures import ThreadPoolExecutor
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


def saved(db, version=1, raw=None):
    raw = raw or raw_factory()
    plan = solve(factory_from_dict(raw), "Balanced")
    payload = {"factory_data": raw, "candidates": [asdict(plan)]}
    db.save_plan("p1", payload, version)
    return payload


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


def test_revision_binding_restart_and_idempotent_publish(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    payload = saved(db)
    request = db.request_approval("p1", "Balanced", expected_version=1)[0]
    with pytest.raises(PermissionError):
        db.decide_approval(request["request_id"], "manager", "manager", "APPROVED")
    db.decide_approval(request["request_id"], "planner", "planner", "APPROVED")
    first = db.publish_plan("p1", "Balanced", "planner", "planner", 1)
    count = db.conn.execute("SELECT COUNT(*) FROM audit_chain").fetchone()[0]
    assert db.publish_plan("p1", "Balanced", "planner", "planner", 1) == first
    assert db.conn.execute("SELECT COUNT(*) FROM audit_chain").fetchone()[0] == count
    db.save_plan("p1", payload, 2)
    with pytest.raises(RuntimeError):
        db.publish_plan("p1", "Balanced", "planner", "planner", 1)
    with pytest.raises(PermissionError):
        db.publish_plan("p1", "Balanced", "planner", "planner", 2)
    db.close()
    restored = Database(path)
    assert restored.get_plan("p1")["version"] == 2
    assert restored.get_plan("p1", 1)["payload"] == payload
    assert restored.verify_audit()
    restored.close()


def test_concurrent_writers_cannot_lose_updates(tmp_path):
    path = tmp_path / "state.db"
    first, second = Database(path), Database(path)
    first.save_plan("p", {"value": 1}, 1)
    def update(db):
        try:
            db.save_plan("p", {"value": 2}, 2)
            return True
        except RuntimeError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, (first, second))) == [False, True]
    assert first.verify_audit()
    first.close()
    second.close()


def test_atomic_failed_approval_and_tampered_revision(tmp_path):
    db = Database(tmp_path / "state.db")
    saved(db)
    with pytest.raises(ValueError):
        db.request_approval("p1", "Balanced", ["add_overtime"])
    assert db.conn.execute("SELECT COUNT(*) FROM bound_approvals").fetchone()[0] == 0
    db.conn.execute("UPDATE revisions SET payload='{}'")
    with pytest.raises(RuntimeError, match="digest"):
        db.request_approval("p1", "Balanced")
    db.close()


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


def test_backup_is_online_and_missing_source_is_not_created(tmp_path):
    db = Database(tmp_path / "state.db")
    saved(db)
    destination = tmp_path / "nested" / "backup.db"
    backup_database(tmp_path / "state.db", destination)
    copy = Database(destination)
    assert copy.get_plan("p1") == db.get_plan("p1") and copy.verify_audit()
    copy.close()
    with pytest.raises(ValueError):
        backup_database(tmp_path / "state.db", tmp_path / "state.db")
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "missing.db", destination)
    assert not (tmp_path / "missing.db").exists()
    db.close()


def test_http_authentication_and_full_publish_flow(tmp_path):
    spec = importlib.util.spec_from_file_location("planning_api", ROOT / "tools/api_server.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    db = Database(tmp_path / "state.db")
    secret = "test-secret-that-is-at-least-32-characters"
    server = api.Server(("127.0.0.1", 0), db, secret, tmp_path)
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
        status, plan = call("/schedule", {"factory_data": raw_factory()})
        assert status == 200
        binding = {"plan_id": plan["plan_id"], "candidate_id": "Balanced", "expected_version": 1}
        assert call("/publish", binding)[0] == 403
        status, approvals = call("/approval/request", binding)
        assert status == 200
        rid = approvals["approvals"][0]["request_id"]
        assert call("/approval/decide", {"request_id": rid, "decision": "APPROVED", "actor": "planner", "role": "planner"}, "manager")[0] == 403
        assert call("/approval/decide", {"request_id": rid, "decision": "APPROVED", "actor": "forged"})[1]["decided_by"] == "planner"
        assert call("/publish", binding)[1]["status"] == "PUBLISHED"
        assert call("/plans?plan_id=" + plan["plan_id"])[1]["version"] == 1
        assert call("/metrics")[1]["schedule"]["count"] >= 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()
