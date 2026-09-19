"""P0-4 production workbook migration and immutable state references."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from planpilot.authority import RuntimeAuthority
from planpilot.factory_state import FactoryStateRegistry, _read, normalize_workbook
from planpilot.persistence import Database
from planpilot.runtime_planning import generate_authoritative_plans_from_state
from planpilot.validation.schema import is_tool_payload_valid


ROOT = Path(__file__).resolve().parents[2]
WORKBOOK = ROOT / "data" / "PlanPilot_Mock_Factory_Dataset.xlsx"


def test_all_17_sheets_migrate_and_contract_tool_outputs_validate(tmp_path):
    db = Database(tmp_path / "factory.db")
    try:
        registry = FactoryStateRegistry(db)
        loaded = registry.load_workbook(WORKBOOK)
        validation = registry.validate(loaded["state_id"])
        state = registry.planning_state(loaded["state_id"])
        assert len(loaded["entity_counts"]) == 17
        assert loaded["entity_counts"]["Routing"] == 6
        assert validation["status"] == "VALID_WITH_QUARANTINE"
        assert [(i["code"], i["entity_id"]) for i in validation["quarantined_entities"]] == [("PROMPT_INJECTION", "EVT-005")]
        assert state["planning_start"] == "2026-09-14T00:00:00+08:00"
        assert state["horizon"] == 7200
        assert all(type(batch["quantity"]) is int for material in state["inventory"].values() for batch in material["batches"])
        assert is_tool_payload_valid(loaded, "load_factory_state", "output_schema")
        assert is_tool_payload_valid(validation, "validate_factory_state", "output_schema")
    finally:
        db.close()


def _mutated(mutator):
    tables, _, digest = _read(WORKBOOK)
    tables = deepcopy(tables)
    mutator(tables)
    with patch("planpilot.factory_state._read", return_value=(tables, [], digest)):
        return normalize_workbook(WORKBOOK)


def test_bad_routing_transitively_quarantines_dependent_orders_only():
    result = _mutated(lambda tables: tables["Routing"][0].__setitem__("duration_min", 0))
    quarantined = {(row["code"], row["entity_type"], row["entity_id"]) for row in result["validation"]["quarantined_entities"]}
    assert ("ROUTING_INVALID", "routing", "BRACKET-A:1") in quarantined
    assert ("QUARANTINE_DEPENDENCY", "order", "ORD-1001") in quarantined
    assert ("QUARANTINE_DEPENDENCY", "order", "ORD-URGENT") in quarantined
    assert [row["order_id"] for row in result["state"]["orders"]] == ["ORD-1002"]
    assert result["validation"]["status"] == "VALID_WITH_QUARANTINE"


def test_invalid_inventory_bucket_is_quarantined_without_discarding_valid_sibling_stock():
    result = _mutated(lambda tables: tables["Inventory"][0].__setitem__("material_uom", "KG"))
    ids = {row["entity_id"] for row in result["validation"]["quarantined_entities"]}
    assert "AL-ONHAND-001" in ids
    assert {row["order_id"] for row in result["state"]["orders"]} == {"ORD-1001", "ORD-1002", "ORD-URGENT"}
    assert [row["batch_id"] for row in result["state"]["inventory"]["AL-6061"]["batches"]] == ["AL-INBOUND-001"]


def test_structural_corruption_is_invalid_not_record_quarantine():
    tables, _, digest = _read(WORKBOOK)
    issue = {"code": "REQUIRED_FIELD_MISSING", "severity": "ERROR", "entity_type": "workbook", "entity_id": None, "field": "Routing", "message": "required sheet Routing is missing"}
    with patch("planpilot.factory_state._read", return_value=(tables, [issue], digest)):
        result = normalize_workbook(WORKBOOK)
    assert result["validation"]["status"] == "INVALID"
    assert result["validation"]["quarantined_entities"] == []


def test_state_id_is_stable_and_sqlite_record_is_immutable(tmp_path):
    db = Database(tmp_path / "factory.db")
    try:
        registry = FactoryStateRegistry(db)
        first = registry.load_workbook(WORKBOOK)
        second = registry.load_workbook(WORKBOOK)
        assert first == second
        assert db.conn.execute("SELECT COUNT(*) FROM factory_states").fetchone()[0] == 1
        record = registry.get(first["state_id"])
        changed = deepcopy(record["state"]); changed["orders"][0]["quantity"] += 1
        replacement = registry.register_normalized(changed)
        assert replacement["state_id"] != first["state_id"]
        assert registry.get(first["state_id"])["state"]["orders"][0]["quantity"] != changed["orders"][0]["quantity"]
    finally:
        db.close()


def test_planning_runtime_resolves_state_reference(tmp_path):
    db = Database(tmp_path / "factory.db")
    try:
        registry = FactoryStateRegistry(db)
        state_id = registry.load_workbook(WORKBOOK)["state_id"]
        result = generate_authoritative_plans_from_state(state_id, registry, RuntimeAuthority(db))
        assert result["state_id"] == state_id
        assert result["stored_plan_count"] == 3
        assert all(option["quarantined_entity_count"] == 1 for option in result["plan_options"])
    finally:
        db.close()
