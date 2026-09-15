"""Fail-closed structural validator for the generated V1.8 workbook."""
import json
from pathlib import Path
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
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
        return {"sheets": len(wb.sheetnames), "events": len(events), "eval_cases": len(evals), "products": len(products), "routing": len(routing), "inventory": len(inventory)}
    finally:
        wb.close()


if __name__ == "__main__":
    print(json.dumps(validate(), ensure_ascii=False))
