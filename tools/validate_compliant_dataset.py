"""Structural checks plus the production V1.8 importer round trip."""
import json
from pathlib import Path
import sys
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
EXPECTED = {"README", "Assumptions", "Machines", "Workers", "Products", "Routing", "Inventory", "Orders", "Events", "Baseline Schedule", "Objectives", "Approval Policy", "Evaluation Cases", "Plan Output Schema", "Shift Calendar", "Worker Skills", "Changeovers"}


def validate(path=ROOT / "data" / "PlanPilot_Mock_Factory_Dataset.xlsx"):
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if set(wb.sheetnames) != EXPECTED:
            raise ValueError(f"sheet set mismatch: {set(wb.sheetnames) ^ EXPECTED}")
        def records(name):
            rows = iter(wb[name].values)
            headers = list(next(rows))
            return [dict(zip(headers, row)) for row in rows if any(x is not None for x in row)]
        events = records("Events")
        if {r["event_id"] for r in events} != {f"EVT-{i:03d}" for i in range(1, 8)}:
            raise ValueError("Events must contain EVT-001 through EVT-007 exactly")
        evals = records("Evaluation Cases")
        if {r["case_id"] for r in evals} != {f"EVAL-{i:03d}" for i in range(1, 31)}:
            raise ValueError("Evaluation Cases must contain EVAL-001 through EVAL-030 exactly")
        products = records("Products")
        if any(type(r["max_lot_size"]) is not int or r["max_lot_size"] <= 0 or r["material_uom"] not in ("EA", "G", "ML") for r in products):
            raise ValueError("Products contains invalid lot or material UOM")
        routing = records("Routing")
        for product in {r["product_id"] for r in routing}:
            if not any(r["product_id"] == product and r["operation_type"] == "INSPECTION" for r in routing):
                raise ValueError(f"routing for {product} lacks terminal inspection")
        skills = records("Worker Skills")
        if any(not 1 <= r["proficiency_level"] <= 5 or type(r["is_primary"]) is not bool for r in skills):
            raise ValueError("Worker Skills contains invalid proficiency or primary flag")
        inventory = records("Inventory")
        if any(type(r["quantity_base_units"]) is not int or r["quantity_base_units"] < 0 or not r["source_id"] for r in inventory):
            raise ValueError("Inventory contains invalid integer source bucket")
        structural = {"sheets": len(wb.sheetnames), "events": len(events), "eval_cases": len(evals), "products": len(products), "routing": len(routing), "inventory": len(inventory)}
    finally:
        wb.close()
    from planpilot.factory_state import FactoryStateRegistry
    from planpilot.persistence import Database, canonical
    database = Database(":memory:")
    try:
        registry = FactoryStateRegistry(database)
        first = registry.load_workbook(path)
        second = registry.load_workbook(path)
        if first != second:
            raise ValueError("production importer did not produce a stable state reference")
        record = registry.get(first["state_id"])
        if record["validation"]["status"] == "INVALID":
            raise ValueError("production importer marked generated workbook INVALID")
        if canonical(record["state"]) != canonical(registry.planning_state(first["state_id"])):
            raise ValueError("factory state round trip changed normalized content")
        return {**structural, "state_id": first["state_id"],
                "import_status": record["validation"]["status"],
                "quarantined_entities": len(record["validation"]["quarantined_entities"])}
    finally:
        database.close()


if __name__ == "__main__":
    print(json.dumps(validate(), ensure_ascii=False))
