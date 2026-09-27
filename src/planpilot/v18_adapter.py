"""Deterministic compact-solver output adapter for explicitly declared V1.8 state.

The adapter authors candidate content.  It never validates its own semantics;
``independent_validator`` performs that separate authority step.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from math import ceil

from .domain.lots import split_lots
from .store import canonical_plan_digest
from .validation.schema import validate


def _origin(state: dict) -> datetime:
    value = datetime.fromisoformat(state["planning_start"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("planning_start must include a timezone")
    return value


def _timestamp(origin: datetime, minute: int) -> str:
    return (origin + timedelta(minutes=minute)).isoformat(timespec="seconds")


def validate_generation_state(state: dict) -> None:
    """Reject missing authority data instead of inventing defaults."""
    required = ("planning_start", "horizon", "orders", "products", "inventory", "machines", "workers", "shifts", "changeovers", "assumptions")
    missing = [key for key in required if key not in state]
    if missing:
        raise ValueError("V1.8 generation state is missing: " + ", ".join(missing))
    product_ids = {row["product_id"] for row in state["products"]}
    if any(order["product_id"] not in product_ids for order in state["orders"]):
        raise ValueError("order references an undeclared product")
    for product in state["products"]:
        if type(product.get("max_lot_size")) is not int or product["max_lot_size"] < 1:
            raise ValueError("every product must declare a positive max_lot_size")
        if product.get("requires_material") and not product.get("bom"):
            raise ValueError("material-requiring product must declare BOM rows")
        for bom in product.get("bom", []):
            material = state["inventory"].get(bom["material_id"])
            if material is None or material.get("material_uom") != bom["material_uom"]:
                raise ValueError("BOM material UOM must match a declared inventory material")
    for order in state["orders"]:
        for operation in order["operations"]:
            for key in ("operation_no", "operation_type", "machine_id", "worker_id", "duration", "required_skill"):
                if key not in operation:
                    raise ValueError(f"routing operation must declare {key}")
    for row in state["shifts"]:
        for key in ("calendar_window_id", "window_type", "start", "end", "overtime_allowed"):
            if key not in row:
                raise ValueError(f"shift row must declare {key}")
        if (row["window_type"] == "OVERTIME") != (row["overtime_allowed"] is True):
            raise ValueError("overtime_allowed must be true exactly for OVERTIME windows")
    assumptions = state["assumptions"]
    for key in ("overtime_cap_hours", "max_overtime_min_per_worker_per_day", "changeover_reference_min", "stability_drift_min"):
        if key not in assumptions:
            raise ValueError(f"assumptions must declare {key}")
    if assumptions.get("allow_optional_lot_splitting") is True:
        raise ValueError("OPTIONAL_LOT_SPLITTING_UNSUPPORTED")


def build_material_reservations(state: dict) -> list[dict]:
    origin = _origin(state)
    products = {p["product_id"]: p for p in state["products"]}
    stock = {
        material_id: sorted(
            [dict(row) for row in material["batches"]],
            key=lambda row: (row["available_at"], 0 if row["source_type"] == "ON_HAND" else 1, row["batch_id"]),
        )
        for material_id, material in state["inventory"].items()
    }
    output = []
    lots = []
    for order in state["orders"]:
        product = products[order["product_id"]]
        for lot in split_lots(order["order_id"], order["quantity"], product["max_lot_size"]):
            lots.append((order, product, lot.lot_no, lot.quantity))
    lots.sort(key=lambda item: (-item[0].get("priority", 0), item[0]["due_at"], item[0]["order_id"], item[2]))
    for rank, (order, product, lot_no, lot_quantity) in enumerate(lots, 1):
        tentative = deepcopy(stock)
        lines, shortage = [], False
        for bom in sorted(product.get("bom", []), key=lambda row: row["material_id"]):
            required = lot_quantity * bom["quantity_per_unit"]
            remaining, allocations = required, []
            for bucket in tentative.get(bom["material_id"], []):
                used = min(remaining, bucket["quantity"])
                if used:
                    allocations.append({
                        "source_type": bucket["source_type"], "source_id": bucket["batch_id"],
                        "available_at": _timestamp(origin, bucket["available_at"]),
                        "quantity_base_units": used,
                    })
                    bucket["quantity"] -= used
                    remaining -= used
            shortage = shortage or remaining > 0
            lines.append({
                "material_id": bom["material_id"], "material_uom": bom["material_uom"],
                "required_quantity_base_units": required,
                "reserved_quantity_base_units": required - remaining,
                "allocations": allocations,
            })
        if shortage:
            for line in lines:
                line["reserved_quantity_base_units"] = 0
                line["allocations"] = []
            ready = None
        else:
            stock = tentative
            ready = max(
                (bucket["available_at"] for line in lines for bucket in line["allocations"]),
                default=_timestamp(origin, state.get("as_of_minute", 0)),
            )
        output.append({
            "order_id": order["order_id"], "product_id": order["product_id"], "lot_no": lot_no,
            "lot_quantity": lot_quantity, "priority_rank": rank,
            "status": "SHORTAGE" if shortage else "READY",
            "earliest_material_ready_time": ready, "lines": lines,
        })
    return output


def _calendar_rows(state: dict, machine_id: str, worker_id: str, start: int, end: int) -> list[dict]:
    return sorted(
        [row for row in state["shifts"] if (row.get("machine_id") == machine_id or row.get("worker_id") == worker_id) and row["start"] < end and start < row["end"]],
        key=lambda row: row["calendar_window_id"],
    )


def _overtime(start: int, end: int, worker_rows: list[dict]) -> int:
    return sum(
        not any(row["window_type"] == "REGULAR" and row["start"] <= minute < row["end"] for row in worker_rows)
        and any(row["window_type"] == "OVERTIME" and row["start"] <= minute < row["end"] for row in worker_rows)
        for minute in range(start, end)
    )


def _skill(state: dict, worker_id: str, skill_id: str) -> dict:
    worker = next(row for row in state["workers"] if row["worker_id"] == worker_id)
    return next(row for row in worker["skills"] if row["skill_id"] == skill_id)


def _operations(candidate: dict, state: dict) -> list[dict]:
    origin = _origin(state)
    orders = {row["order_id"]: row for row in state["orders"]}
    routing = {(order["order_id"], op["operation_no"]): op for order in state["orders"] for op in order["operations"]}
    transitions = {(row["machine_id"], row["from_product_id"], row["to_product_id"]): row["minutes"] for row in state["changeovers"]}
    previous_product = {}
    output = []
    for row in sorted(candidate["operations"], key=lambda item: (item["start"], item["machine_id"], item["order_id"], item["operation_no"])):
        order, route = orders[row["order_id"]], routing[(row["order_id"], row["operation_no"])]
        product = next(product for product in state["products"] if product["product_id"] == order["product_id"])
        lot_count = ceil(order["quantity"] / product["max_lot_size"])
        lot_no = row.get("lot_no", 1)
        lot_quantity = product["max_lot_size"] if lot_no < lot_count else order["quantity"] - product["max_lot_size"] * (lot_count - 1)
        windows = _calendar_rows(state, row["machine_id"], row["worker_id"], row["start"], row["end"])
        worker_windows = [window for window in windows if window.get("worker_id") == row["worker_id"]]
        prior = previous_product.get(row["machine_id"])
        changeover = 0 if prior is None else transitions[(row["machine_id"], prior, order["product_id"])]
        previous_product[row["machine_id"]] = order["product_id"]
        skill = _skill(state, row["worker_id"], route["required_skill"])
        reason = "PRIMARY_SKILL_MATCH" if skill["is_primary"] else "SECONDARY_SKILL_MATCH_REQUIRES_APPROVAL"
        overtime = _overtime(row["start"], row["end"], worker_windows)
        codes = [reason]
        if overtime:
            codes.append("OVERTIME_USED_REQUIRES_APPROVAL")
        if route["operation_type"] == "INSPECTION":
            codes.append("INSPECTION_REQUIRED")
        output.append({
            "order_id": order["order_id"], "product_id": order["product_id"], "lot_no": lot_no,
            "lot_quantity": lot_quantity, "operation_no": route["operation_no"],
            "operation_type": route["operation_type"], "machine_id": row["machine_id"],
            "worker_id": row["worker_id"], "start_time": _timestamp(origin, row["start"]),
            "end_time": _timestamp(origin, row["end"]),
            "calendar_window_ids": [window["calendar_window_id"] for window in windows],
            "duration_min": route["duration"], "overtime_min": overtime,
            "changeover_min": changeover,
            "risk_level": "Medium" if overtime or not skill["is_primary"] else "Low",
            "decision_reason_codes": codes,
            "decision_summary": "; ".join(codes),
        })
    return output


def _unscheduled(candidate: dict, state: dict) -> list[dict]:
    orders = {row["order_id"]: row for row in state["orders"]}
    reservations = {(row["order_id"], row["lot_no"]): row for row in build_material_reservations(state)}
    result = []
    for row in candidate["unscheduled_operations"]:
        order = orders[row["order_id"]]
        lot_no = row.get("lot_no", 1)
        reason = "MATERIAL_SHORTAGE" if reservations[(order["order_id"], lot_no)]["status"] == "SHORTAGE" else "CAPACITY"
        result.append({
            "order_id": order["order_id"], "product_id": order["product_id"], "lot_no": lot_no,
            "operation_no": row["operation_no"], "reason_code": reason,
            "reason": "Required material is unavailable." if reason == "MATERIAL_SHORTAGE" else "No feasible capacity was found inside the declared horizon.",
        })
    return sorted(result, key=lambda row: (row["order_id"], row["lot_no"], row["operation_no"]))


def _kpis(operations: list[dict], unscheduled: list[dict], state: dict,
          reference_content: dict | None = None) -> dict:
    origin = _origin(state)
    by_order = {row["order_id"]: [] for row in state["orders"]}
    for row in operations:
        by_order[row["order_id"]].append(row)
    completion, complete = {}, set()
    for order in state["orders"]:
        rows = by_order[order["order_id"]]
        product = next(product for product in state["products"] if product["product_id"] == order["product_id"])
        expected_count = ceil(order["quantity"] / product["max_lot_size"]) * len(order["operations"])
        if len(rows) == expected_count:
            complete.add(order["order_id"])
            completion[order["order_id"]] = max(int((_origin_time(row["end_time"]) - origin).total_seconds() // 60) for row in rows)
    on_time = sum(order["order_id"] in complete and completion[order["order_id"]] <= order["due_at"] for order in state["orders"])
    total = len(state["orders"])
    stability = 1.0
    if reference_content is not None:
        current = {(row["order_id"], row["lot_no"], row["operation_no"]): row for row in operations}
        reference = {(row["order_id"], row["lot_no"], row["operation_no"]): row
                     for row in reference_content["operations"]}
        union = set(current) | set(reference)
        unchanged = 0
        for key in union:
            if key not in current or key not in reference:
                continue
            left, right = current[key], reference[key]
            fields = ("product_id", "lot_quantity", "operation_type", "duration_min", "machine_id", "worker_id")
            if all(left[field] == right[field] for field in fields) and abs(
                int((_origin_time(left["start_time"]) - origin).total_seconds() // 60)
                - int((_origin_time(right["start_time"]) - origin).total_seconds() // 60)
            ) <= state["assumptions"]["stability_drift_min"]:
                unchanged += 1
        stability = unchanged / len(union) if union else 1.0
    return {
        "on_time_rate": on_time / total if total else 0,
        "eligible_orders": total, "on_time_orders": on_time,
        "eligible_order_coverage_rate": len(complete) / total if total else 1.0,
        "late_orders": total - on_time,
        "total_tardiness_min": sum(max(0, completion.get(order["order_id"], state["horizon"]) - order["due_at"]) for order in state["orders"]),
        "overtime_hours": sum(row["overtime_min"] for row in operations) / 60,
        "changeover_count": sum(row["changeover_min"] > 0 for row in operations),
        "total_changeover_min": sum(row["changeover_min"] for row in operations),
        "schedule_stability": stability,
        "unscheduled_operations": len(unscheduled),
        "secondary_skill_assignment_count": sum("SECONDARY_SKILL_MATCH_REQUIRES_APPROVAL" in row["decision_reason_codes"] for row in operations),
    }


def _origin_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_plan_content(candidate: dict, state: dict, plan_id: str, plan_version: int,
                       reference_content: dict | None = None) -> dict:
    validate_generation_state(state)
    operations = _operations(candidate, state)
    unscheduled = _unscheduled(candidate, state)
    kpis = _kpis(operations, unscheduled, state, reference_content)
    assumptions = state["assumptions"]
    overtime_cap = assumptions["overtime_cap_hours"]
    overtime_score = (
        1.0 if kpis["overtime_hours"] == 0 else 0.0
    ) if overtime_cap == 0 else 1 - min(kpis["overtime_hours"] / overtime_cap, 1)
    changeover_score = 1 - min(kpis["total_changeover_min"] / assumptions["changeover_reference_min"], 1)
    content = {
        "plan_id": plan_id, "plan_version": plan_version, "plan_digest": "0" * 64,
        "profile": candidate["profile"], "operations": operations,
        "unscheduled_operations": unscheduled, "kpis": kpis,
        "engine": {
            "solver": "PRIORITY_DISPATCH_FALLBACK" if candidate["solver_status"] == "HEURISTIC_FALLBACK" else "CP-SAT",
            "solver_status": candidate["solver_status"] if candidate["solver_status"] in ("OPTIMAL", "HEURISTIC_FALLBACK") else "FEASIBLE",
            "random_seed": 42, "time_budget_seconds": 40,
            "deterministic_budget": float(candidate.get("deterministic_budget", 0.0)),
            "canonical_plan_hash": "0" * 64, "objective_value": None,
            "normalized_scores": {
                "delivery_score": kpis["on_time_rate"], "overtime_score": overtime_score,
                "changeover_score": changeover_score, "stability_score": kpis["schedule_stability"],
            },
        },
        "stability_reference_plan_id": None if reference_content is None else reference_content["plan_id"],
        "assumptions": [
            f'Planning horizon starts at {state["planning_start"]}.',
            "All capacity, skills, BOM rows, material buckets and changeovers are declared in the loaded factory state.",
        ],
        "consequential_approval_required": bool(kpis["overtime_hours"] or kpis["secondary_skill_assignment_count"]),
        "publish_confirmation_required": True,
        "infeasible_reason": None if not unscheduled else "Eligible routing operations remain unscheduled.",
        "material_reservations": build_material_reservations(state),
    }
    digest = canonical_plan_digest(content)
    content["plan_digest"] = digest
    content["engine"]["canonical_plan_hash"] = digest
    validate(content, "plan_content", "plan_content", plan_id)
    return content
