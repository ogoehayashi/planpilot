"""Independent V1.8 plan validation.

This module deliberately does not import the generator, compact solver, or
contract adapter.  It rebuilds lots, material reservations, calendar coverage,
hard-constraint results and KPIs from authoritative factory-state data.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from math import ceil

from .store import canonical_plan_digest
from .validation.schema import validate, validate_tool_payload


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("factory and plan timestamps must include a timezone")
    return parsed


def _minute(origin: datetime, value: str) -> int:
    seconds = (_dt(value) - origin).total_seconds()
    if seconds % 60:
        raise ValueError("plan timestamps must resolve to whole minutes")
    return int(seconds // 60)


def _timestamp(origin: datetime, minute: int) -> str:
    return (origin + timedelta(minutes=minute)).isoformat(timespec="seconds")


def _lots(state: dict) -> list[dict]:
    products = {p["product_id"]: p for p in state["products"]}
    lots = []
    for order in sorted(state["orders"], key=lambda row: row["order_id"]):
        product = products[order["product_id"]]
        maximum = product["max_lot_size"]
        count = ceil(order["quantity"] / maximum)
        for lot_no in range(1, count + 1):
            quantity = maximum if lot_no < count else order["quantity"] - maximum * (count - 1)
            lots.append({**order, "lot_no": lot_no, "lot_quantity": quantity, "product": product})
    return lots


def _inventory(state: dict) -> dict[str, list[dict]]:
    result = {}
    for material_id, material in state["inventory"].items():
        rows = []
        for row in material["batches"]:
            rows.append({
                "source_type": row["source_type"],
                "source_id": row["batch_id"],
                "available_at_min": row["available_at"],
                "quantity": row["quantity"],
            })
        result[material_id] = sorted(
            rows,
            key=lambda row: (
                row["available_at_min"],
                0 if row["source_type"] == "ON_HAND" else 1,
                row["source_id"],
            ),
        )
    return result


def recompute_material_reservations(state: dict) -> list[dict]:
    """Re-run the contract's atomic, single-pass lot reservation algorithm."""
    origin = _dt(state["planning_start"])
    stock = _inventory(state)
    lots = sorted(
        _lots(state),
        key=lambda row: (-row.get("priority", 0), row["due_at"], row["order_id"], row["lot_no"]),
    )
    output = []
    for rank, lot in enumerate(lots, 1):
        tentative = deepcopy(stock)
        lines = []
        shortage = False
        for bom in sorted(lot["product"].get("bom", []), key=lambda row: row["material_id"]):
            required = lot["lot_quantity"] * bom["quantity_per_unit"]
            remaining = required
            allocations = []
            for bucket in tentative.get(bom["material_id"], []):
                used = min(remaining, bucket["quantity"])
                if used:
                    allocations.append({
                        "source_type": bucket["source_type"],
                        "source_id": bucket["source_id"],
                        "available_at": _timestamp(origin, bucket["available_at_min"]),
                        "quantity_base_units": used,
                    })
                    bucket["quantity"] -= used
                    remaining -= used
            shortage = shortage or remaining > 0
            lines.append({
                "material_id": bom["material_id"],
                "material_uom": bom["material_uom"],
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
            ready_min = max(
                (bucket["available_at_min"] for line in lines for bucket in _inventory_line_minutes(line, origin)),
                default=0,
            )
            ready = _timestamp(origin, ready_min)
        output.append({
            "order_id": lot["order_id"],
            "product_id": lot["product_id"],
            "lot_no": lot["lot_no"],
            "lot_quantity": lot["lot_quantity"],
            "priority_rank": rank,
            "status": "SHORTAGE" if shortage else "READY",
            "earliest_material_ready_time": ready,
            "lines": lines,
        })
    return output


def _inventory_line_minutes(line: dict, origin: datetime):
    for allocation in line["allocations"]:
        yield {"available_at_min": _minute(origin, allocation["available_at"])}


def _skill(state: dict, worker_id: str, skill_id: str) -> dict | None:
    worker = next((row for row in state["workers"] if row["worker_id"] == worker_id), None)
    if worker is None:
        return None
    return next((row for row in worker["skills"] if row["skill_id"] == skill_id), None)


def _windows(state: dict, row: dict, origin: datetime) -> tuple[list[dict], list[dict]]:
    start, end = _minute(origin, row["start_time"]), _minute(origin, row["end_time"])
    machine = [w for w in state["shifts"] if w.get("machine_id") == row["machine_id"] and w["start"] < end and start < w["end"]]
    worker = [w for w in state["shifts"] if w.get("worker_id") == row["worker_id"] and w["start"] < end and start < w["end"]]
    return machine, worker


def _covered(start: int, end: int, windows: list[dict]) -> bool:
    cursor = start
    for row in sorted(windows, key=lambda item: (item["start"], item["end"], item["calendar_window_id"])):
        if row["end"] <= cursor:
            continue
        if row["start"] > cursor:
            return False
        cursor = max(cursor, row["end"])
        if cursor >= end:
            return True
    return cursor >= end


def _worker_overtime(start: int, end: int, windows: list[dict]) -> int:
    total = 0
    for minute in range(start, end):
        regular = any(w["window_type"] == "REGULAR" and w["start"] <= minute < w["end"] for w in windows)
        overtime = any(w["window_type"] == "OVERTIME" and w["start"] <= minute < w["end"] for w in windows)
        if not regular and overtime:
            total += 1
    return total


def _worker_overtime_by_date(origin: datetime, start: int, end: int, windows: list[dict]) -> dict[str, int]:
    result = defaultdict(int)
    for minute in range(start, end):
        regular = any(w["window_type"] == "REGULAR" and w["start"] <= minute < w["end"] for w in windows)
        overtime = any(w["window_type"] == "OVERTIME" and w["start"] <= minute < w["end"] for w in windows)
        if not regular and overtime:
            result[(origin + timedelta(minutes=minute)).date().isoformat()] += 1
    return result


def _issue(code: str, hc: str, row: dict | None, message: str, field: str | None = None) -> dict:
    return {
        "code": code,
        "severity": "ERROR",
        "entity_type": "schedule_operation",
        "entity_id": None if row is None else f'{row.get("order_id")}:{row.get("lot_no")}:{row.get("operation_no")}',
        "field": field,
        "message": f"{hc}: {message}",
    }


def _expected(state: dict):
    result = {}
    for lot in _lots(state):
        for operation in lot["operations"]:
            result[(lot["order_id"], lot["lot_no"], operation["operation_no"])] = (lot, operation)
    return result


def _changeover_map(state: dict) -> dict[tuple[str, str, str], int]:
    return {
        (row["machine_id"], row["from_product_id"], row["to_product_id"]): row["minutes"]
        for row in state["changeovers"]
    }


def hard_constraint_violations(content: dict, state: dict) -> list[dict]:
    """Evaluate HC-001 through HC-013 from state, without generator helpers."""
    origin = _dt(state["planning_start"])
    expected = _expected(state)
    reservations = recompute_material_reservations(state)
    reservation_by_lot = {(r["order_id"], r["lot_no"]): r for r in reservations}
    operations = content["operations"]
    unscheduled = content["unscheduled_operations"]
    violations = []
    scheduled_keys = set()

    for row in operations:
        key = (row["order_id"], row["lot_no"], row["operation_no"])
        scheduled_keys.add(key)
        pair = expected.get(key)
        if pair is None:
            violations.append(_issue("ROUTING_INVALID", "HC-012", row, "operation is not in the eligible routing set"))
            continue
        lot, route = pair
        start, end = _minute(origin, row["start_time"]), _minute(origin, row["end_time"])
        if row["product_id"] != lot["product_id"] or row["lot_quantity"] != lot["lot_quantity"]:
            violations.append(_issue("LOT_QUANTITY_MISMATCH", "HC-009", row, "stored lot identity or quantity disagrees with fixed decomposition"))
        if row["operation_type"] != route["operation_type"]:
            violations.append(_issue("ROUTING_INVALID", "HC-004", row, "operation type disagrees with routing", "operation_type"))
        if end - start != row["duration_min"] or row["duration_min"] != route["duration"]:
            violations.append(_issue("INVALID_VALUE", "HC-008", row, "duration and contiguous interval disagree", "duration_min"))
        if row["machine_id"] != route["machine_id"] or row["worker_id"] != route["worker_id"]:
            violations.append(_issue("ROUTING_INVALID", "HC-004", row, "assignment disagrees with routing"))
        skill = _skill(state, row["worker_id"], route["required_skill"])
        if skill is None or skill["proficiency_level"] < 2:
            violations.append(_issue("WORKER_AVAILABILITY_INVALID", "HC-003", row, "worker lacks required proficiency", "worker_id"))
        reservation = reservation_by_lot[key[:2]]
        ready = None if reservation["earliest_material_ready_time"] is None else _minute(origin, reservation["earliest_material_ready_time"])
        if reservation["status"] != "READY" or (route["operation_type"] == "PRODUCTION" and route["operation_no"] == 1 and start < ready):
            violations.append(_issue("MATERIAL_RESERVATION_MISMATCH", "HC-005", row, "material is not ready before first production operation"))
        for block in state.get("maintenance", []):
            same = block.get("machine_id") == row["machine_id"] or block.get("worker_id") == row["worker_id"]
            if same and start < block["end"] and block["start"] < end:
                violations.append(_issue("MACHINE_CAPACITY_INVALID", "HC-006", row, "operation intersects declared unavailability"))
        machine_windows, worker_windows = _windows(state, row, origin)
        referenced = {w["calendar_window_id"] for w in machine_windows + worker_windows}
        if not _covered(start, end, machine_windows) or not _covered(start, end, worker_windows) or set(row["calendar_window_ids"]) != referenced:
            violations.append(_issue("SHIFT_WINDOW_REFERENCE_INVALID", "HC-007", row, "calendar references do not exactly cover both resources", "calendar_window_ids"))
        overtime = _worker_overtime(start, end, worker_windows)
        if row["overtime_min"] != overtime:
            violations.append(_issue("OVERTIME_WINDOW_UNDEFINED", "HC-013", row, "stored overtime minutes disagree with worker calendars", "overtime_min"))

    for field, hc, code in (("machine_id", "HC-001", "MACHINE_CAPACITY_INVALID"), ("worker_id", "HC-002", "WORKER_AVAILABILITY_INVALID")):
        groups = defaultdict(list)
        for row in operations:
            groups[row[field]].append(row)
        for rows in groups.values():
            rows.sort(key=lambda row: (_minute(origin, row["start_time"]), row["order_id"], row["lot_no"], row["operation_no"]))
            for left, right in zip(rows, rows[1:]):
                left_end = _minute(origin, left["end_time"])
                right_start = _minute(origin, right["start_time"])
                occupied_start = right_start - right["changeover_min"] if field == "machine_id" else right_start
                if left_end > occupied_start:
                    violations.append(_issue(code, hc, right, f"{field} assignments overlap"))

    by_lot = defaultdict(list)
    for row in operations:
        by_lot[(row["order_id"], row["lot_no"])].append(row)
    for rows in by_lot.values():
        rows.sort(key=lambda row: row["operation_no"])
        for left, right in zip(rows, rows[1:]):
            if _minute(origin, left["end_time"]) > _minute(origin, right["start_time"]):
                violations.append(_issue("ROUTING_INVALID", "HC-004", right, "routing precedence is violated"))

    for lot in _lots(state):
        if lot["lot_quantity"] > lot["product"]["max_lot_size"]:
            violations.append(_issue("LOT_QUANTITY_MISMATCH", "HC-009", None, "lot exceeds max_lot_size"))
        rows = by_lot.get((lot["order_id"], lot["lot_no"]), [])
        terminal = lot["operations"][-1]
        if terminal["operation_type"] != "INSPECTION" or (rows and not any(r["operation_no"] == terminal["operation_no"] for r in rows)):
            violations.append(_issue("ROUTING_INVALID", "HC-010", None, f'{lot["order_id"]} lot {lot["lot_no"]} lacks final inspection'))

    transitions = _changeover_map(state)
    for machine_id in sorted({row["machine_id"] for row in operations}):
        rows = sorted((row for row in operations if row["machine_id"] == machine_id), key=lambda row: (_minute(origin, row["start_time"]), row["order_id"], row["lot_no"], row["operation_no"]))
        for index, row in enumerate(rows):
            expected_minutes = 0 if index == 0 else transitions.get((machine_id, rows[index - 1]["product_id"], row["product_id"]))
            if expected_minutes is None or row["changeover_min"] != expected_minutes:
                violations.append(_issue("CHANGEOVER_TRANSITION_MISSING", "HC-011", row, "changeover row is missing or stored minutes disagree", "changeover_min"))

    unscheduled_keys = {(r["order_id"], r["lot_no"], r["operation_no"]) for r in unscheduled}
    expected_keys = set(expected)
    if scheduled_keys & unscheduled_keys or scheduled_keys | unscheduled_keys != expected_keys:
        violations.append(_issue("ROUTING_INVALID", "HC-012", None, "eligible routing operations are duplicated, omitted, or unknown"))

    daily = defaultdict(int)
    overtime_total = 0
    for row in operations:
        start, end = _minute(origin, row["start_time"]), _minute(origin, row["end_time"])
        _, worker_windows = _windows(state, row, origin)
        by_date = _worker_overtime_by_date(origin, start, end, worker_windows)
        overtime_total += sum(by_date.values())
        for local_date, minutes in by_date.items():
            daily[(row["worker_id"], local_date)] += minutes
    assumptions = state["assumptions"]
    if any(value > assumptions["max_overtime_min_per_worker_per_day"] for value in daily.values()) or overtime_total > assumptions["overtime_cap_hours"] * 60:
        violations.append(_issue("OVERTIME_CAP_EXCEEDED", "HC-013", None, "declared overtime cap is exceeded"))

    if content["material_reservations"] != reservations:
        violations.append(_issue("MATERIAL_RESERVATION_MISMATCH", "HC-005", None, "stored material reservations disagree with independent recomputation"))
    violations.sort(key=lambda row: (row["message"], row["entity_id"] or "", row["field"] or ""))
    return violations


def recompute_kpis(content: dict, state: dict, reference_content: dict | None = None) -> dict:
    origin = _dt(state["planning_start"])
    expected = _expected(state)
    scheduled = {(r["order_id"], r["lot_no"], r["operation_no"]): r for r in content["operations"]}
    complete_orders = set()
    completion = {}
    for order in state["orders"]:
        keys = [key for key in expected if key[0] == order["order_id"]]
        if all(key in scheduled for key in keys):
            complete_orders.add(order["order_id"])
            completion[order["order_id"]] = max(_minute(origin, scheduled[key]["end_time"]) for key in keys)
    on_time = sum(completion.get(row["order_id"], state["horizon"] + row["due_at"] + 1) <= row["due_at"] for row in state["orders"] if row["order_id"] in complete_orders)
    late = len(state["orders"]) - on_time
    tardiness = sum(max(0, completion.get(row["order_id"], state["horizon"]) - row["due_at"]) for row in state["orders"])
    current = scheduled
    if reference_content is None:
        stability = 1.0
    else:
        reference = {(r["order_id"], r["lot_no"], r["operation_no"]): r for r in reference_content["operations"]}
        union = set(current) | set(reference)
        unchanged = 0
        for key in union:
            if key not in current or key not in reference:
                continue
            left, right = current[key], reference[key]
            fields = ("product_id", "lot_quantity", "operation_type", "duration_min", "machine_id", "worker_id")
            if all(left[field] == right[field] for field in fields) and abs(_minute(origin, left["start_time"]) - _minute(origin, right["start_time"])) <= state["assumptions"]["stability_drift_min"]:
                unchanged += 1
        stability = unchanged / len(union) if union else 1.0
    secondary = 0
    for row in content["operations"]:
        _, route = expected[(row["order_id"], row["lot_no"], row["operation_no"])]
        skill = _skill(state, row["worker_id"], route["required_skill"])
        secondary += bool(skill and skill["is_primary"] is False)
    count = len(state["orders"])
    return {
        "on_time_rate": on_time / count if count else 0,
        "eligible_orders": count,
        "on_time_orders": on_time,
        "eligible_order_coverage_rate": len(complete_orders) / count if count else 1.0,
        "late_orders": late,
        "total_tardiness_min": tardiness,
        "overtime_hours": sum(row["overtime_min"] for row in content["operations"]) / 60,
        "changeover_count": sum(row["changeover_min"] > 0 for row in content["operations"]),
        "total_changeover_min": sum(row["changeover_min"] for row in content["operations"]),
        "schedule_stability": stability,
        "unscheduled_operations": len(content["unscheduled_operations"]),
        "secondary_skill_assignment_count": secondary,
    }


def _approval_requirements(kpis: dict) -> list[dict]:
    result = []
    if kpis["secondary_skill_assignment_count"]:
        result.append({"action": "assign_qualified_secondary_skill", "approver_role": "Production Planner", "reason": "The validated plan assigns at least one qualified secondary skill."})
    if kpis["overtime_hours"]:
        result.append({"action": "add_overtime", "approver_role": "Production Manager", "reason": "The validated plan uses declared overtime capacity."})
    result.append({"action": "publish_plan", "approver_role": "Production Planner", "reason": "Every publication requires explicit Production Planner confirmation."})
    return result


def validate_plan_content(
    content: dict, state: dict, reference_content: dict | None = None,
    quarantine_impact: list[str] | None = None,
) -> dict:
    """Fail closed on schema, digest or KPI mismatch; return V1.8 validate_plan output."""
    validate(content, "plan_content", "plan_content", content.get("plan_id"))
    recomputed_digest = canonical_plan_digest(content)
    if content["plan_digest"] != recomputed_digest or content["engine"]["canonical_plan_hash"] != recomputed_digest:
        raise ValueError("PLAN_DIGEST_MISMATCH: canonical digest verification failed")
    recomputed_kpis = recompute_kpis(content, state, reference_content)
    if content["kpis"] != recomputed_kpis:
        raise ValueError("VALIDATION_FAILED: stored KPIs disagree with independent recomputation")
    violations = hard_constraint_violations(content, state)
    feasible = not violations and not content["unscheduled_operations"]
    result = {
        "plan_id": content["plan_id"],
        "plan_version": content["plan_version"],
        "plan_digest": content["plan_digest"],
        "recomputed_plan_digest": recomputed_digest,
        "digest_verified": True,
        "is_feasible": feasible,
        "infeasible_reason": None if feasible else (violations[0]["message"] if violations else "Eligible routing operations remain unscheduled."),
        "hard_violations": violations,
        "kpis": recomputed_kpis,
        "approval_requirements": _approval_requirements(recomputed_kpis),
        "quarantine_impact": list(quarantine_impact or []),
    }
    validate_tool_payload(result, "validate_plan", "output_schema", "validate_plan_output", content["plan_id"])
    return result


def approval_impacts(result: dict, content: dict | None = None) -> dict[str, dict]:
    """Build the server-owned structured impact for each required action."""
    kpis = result["kpis"]
    operations = [] if content is None else content["operations"]
    reasons = {
        "assign_qualified_secondary_skill": "SECONDARY_SKILL_REQUIRED",
        "add_overtime": "OVERTIME_REQUIRED",
        "change_promised_due_date": "PROMISED_DUE_DATE_CHANGE_REQUIRED",
        "publish_plan": "PUBLISH_CONFIRMATION_REQUIRED",
    }
    impacted = {
        "assign_qualified_secondary_skill": [
            operation for operation in operations
            if "SECONDARY_SKILL_MATCH_REQUIRES_APPROVAL" in operation["decision_reason_codes"]
        ],
        "add_overtime": [operation for operation in operations if operation["overtime_min"] > 0],
        "change_promised_due_date": [],
        "publish_plan": operations,
    }
    return {
        row["action"]: {
            "affected_order_ids": sorted({operation["order_id"] for operation in impacted[row["action"]]}),
            "changed_operation_count": len(impacted[row["action"]]),
            "kpi_deltas": {
                "overtime_hours": kpis["overtime_hours"],
                "secondary_skill_assignment_count": kpis["secondary_skill_assignment_count"],
            },
            "reason_codes": [reasons[row["action"]]],
        }
        for row in result["approval_requirements"]
    }
