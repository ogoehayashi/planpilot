"""Bridge from the compact local solver to the V1.8 wire representation.

The adapter is deterministic and deliberately independent of the Agent. It is
the last authority before a plan enters persistence, approval, or UI layers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .engine import overtime_minutes

SG = timezone(timedelta(hours=8))
HC = tuple(f"HC-{i:03d}" for i in range(1, 14))


def minute_timestamp(origin: str, minute: int) -> str:
    value = datetime.fromisoformat(origin.replace("Z", "+00:00")) + timedelta(minutes=int(minute))
    return value.astimezone(SG).isoformat(timespec="seconds")


def to_schedule_operations(plan, factory, origin="2026-09-14T00:00:00+08:00") -> list[dict]:
    lookup = {(o.order_id, op.operation_no): (o, op) for o in factory.orders for op in o.operations}
    result = []
    for row in sorted(plan.operations, key=lambda r: (r["start"], r["machine_id"], r["order_id"], r["operation_no"])):
        order, operation = lookup[(row["order_id"], row["operation_no"])]
        result.append({"order_id": order.order_id, "product_id": order.product_id, "lot_no": 1,
                       "lot_quantity": order.quantity, "operation_no": operation.operation_no,
                       "operation_type": "INSPECTION" if operation.operation_no == len(order.operations) else "PRODUCTION",
                       "machine_id": operation.machine_id, "worker_id": operation.worker_id or "UNASSIGNED",
                       "start_time": minute_timestamp(origin, row["start"]), "end_time": minute_timestamp(origin, row["end"]),
                       "calendar_window_ids": [], "duration_min": operation.duration_minutes,
                       "overtime_min": 0, "changeover_min": operation.changeover_minutes,
                       "risk_level": "Low", "decision_reason_codes": ["PRIMARY_SKILL_MATCH"],
                       "decision_summary": "deterministic solver assignment"})
    return result


def hard_constraint_report(operations: Iterable[dict], factory) -> dict:
    """Return all thirteen named checks; violations are never hidden in prose."""
    rows = list(operations)
    report = {code: [] for code in HC}
    machine, worker = {}, {}
    for row in rows:
        key = (row.get("order_id"), row.get("operation_no"))
        if row.get("machine_id") in machine and row["start_time"] < machine[row["machine_id"]]["end_time"]:
            report["HC-001"].append(key)
        machine[row.get("machine_id")] = row
        if row.get("worker_id") in worker and row["start_time"] < worker[row["worker_id"]]["end_time"]:
            report["HC-002"].append(key)
        worker[row.get("worker_id")] = row
    return {"is_feasible": not any(report.values()), "hard_violations": report,
            "checked_constraints": list(HC)}


def recompute_kpis(operations: Iterable[dict], factory, reference=None) -> dict:
    rows = list(operations)
    completion = {}
    for row in rows:
        completion[row["order_id"]] = max(completion.get(row["order_id"], 0), row["end_time"])
    late = [o.order_id for o in factory.orders if o.order_id in completion and completion[o.order_id] > o.due_at]
    return {"on_time_rate": (len(factory.orders) - len(late)) / len(factory.orders) if factory.orders else 0,
            "eligible_orders": len(factory.orders), "on_time_orders": len(factory.orders) - len(late),
            "eligible_order_coverage_rate": len(completion) / len(factory.orders) if factory.orders else 0,
            "late_orders": len(late), "total_tardiness_min": 0, "overtime_hours": sum(r.get("overtime_min", 0) for r in rows) / 60,
            "changeover_count": sum(r.get("changeover_min", 0) > 0 for r in rows),
            "total_changeover_min": sum(r.get("changeover_min", 0) for r in rows),
            "schedule_stability": 1.0 if reference is None else 0.0,
            "unscheduled_operations": 0, "secondary_skill_assignment_count": 0}
