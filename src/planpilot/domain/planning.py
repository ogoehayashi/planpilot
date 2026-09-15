"""Finite capacity planning with immutable FIFO reservations and independent checks."""
from collections import defaultdict
from copy import deepcopy

from ortools.sat.python import cp_model

from .models import SchedulePlan

PROFILES = ("Balanced", "Delivery First", "Cost First")


def reserve_materials(factory):
    stock = {}
    for material, record in sorted(factory.inventory.items()):
        if isinstance(record, int):
            record = {"quantity": record}
        stock[material] = sorted(deepcopy(record.get("batches", [dict(record, batch_id=material)])),
                                 key=lambda b: (b.get("available_at", 0), b["batch_id"]))
    result = {}
    for order in sorted(factory.orders, key=lambda o: (-o.priority, o.due_at, o.order_id)):
        tentative, allocations, shortage = deepcopy(stock), [], False
        for op in order.operations:
            remaining = op.material_qty
            for batch in tentative.get(op.material_id, []):
                used = min(remaining, batch["quantity"])
                if used:
                    allocations.append({"material_id": op.material_id, "batch_id": batch["batch_id"],
                                        "quantity": used, "available_at": batch.get("available_at", 0)})
                    batch["quantity"] -= used
                    remaining -= used
            shortage |= remaining > 0
        if not shortage:
            stock = tentative
        result[order.order_id] = {"status": "SHORTAGE" if shortage else "READY",
                                 "allocations": [] if shortage else allocations,
                                 "ready_at": None if shortage else max((a["available_at"] for a in allocations), default=0)}
    return result


def windows(factory, op, horizon):
    result = [(0, horizon)]
    for field, resource in (("machine_id", op.machine_id), ("worker_id", op.worker_id)):
        if resource is None:
            continue
        rows = sorted((r["start"], r["end"]) for r in factory.shifts if r.get(field) == resource)
        if not rows:
            if factory.shifts:
                return []
            continue
        merged = []
        for start, end in rows:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        result = [(max(a, c), min(b, d)) for a, b in result for c, d in merged if max(a, c) < min(b, d)]
    return result


def build_candidates(factory):
    return [solve(factory, profile) for profile in PROFILES]


def solve(factory, profile):
    if profile not in PROFILES:
        raise ValueError("unknown profile")
    jobs = [(o, op) for o in sorted(factory.orders, key=lambda o: o.order_id)
            for op in sorted(o.operations, key=lambda op: op.operation_no)]
    horizon = factory.horizon
    reservations = reserve_materials(factory)
    model = cp_model.CpModel()
    presence = {o.order_id: model.NewBoolVar(o.order_id) for o in factory.orders}
    starts, ends, machines, workers, previous = [], [], defaultdict(list), defaultdict(list), {}
    for i, (order, op) in enumerate(jobs):
        active = presence[order.order_id]
        start = model.NewIntVar(0, horizon, f"start_{i}")
        end = model.NewIntVar(0, horizon, f"end_{i}")
        interval = model.NewOptionalIntervalVar(start, op.duration_minutes, end, active, f"op_{i}")
        starts.append(start)
        ends.append(end)
        machines[op.machine_id].append(interval)
        if op.worker_id:
            workers[op.worker_id].append(interval)
        model.Add(start == 0).OnlyEnforceIf(active.Not())
        model.Add(end == 0).OnlyEnforceIf(active.Not())
        reservation = reservations[order.order_id]
        if reservation["status"] == "SHORTAGE" or not qualified(factory, op):
            model.Add(active == 0)
        model.Add(start >= max(op.release_at, reservation["ready_at"] or 0)).OnlyEnforceIf(active)
        if order.order_id in previous:
            model.Add(start >= previous[order.order_id]).OnlyEnforceIf(active)
        previous[order.order_id] = end
        choices = []
        for j, (a, b) in enumerate(windows(factory, op, horizon)):
            choice = model.NewBoolVar(f"window_{i}_{j}")
            choices.append(choice)
            model.Add(start >= a).OnlyEnforceIf(choice)
            model.Add(end <= b).OnlyEnforceIf(choice)
        model.Add(sum(choices) == active)
    for i, row in enumerate(sorted(factory.maintenance, key=lambda r: (r.get("machine_id", ""), r.get("worker_id", ""), r["start"], r["end"]))):
        interval = model.NewIntervalVar(row["start"], row["end"] - row["start"], row["end"], f"block_{i}")
        if row.get("machine_id"):
            machines[row["machine_id"]].append(interval)
        if row.get("worker_id"):
            workers[row["worker_id"]].append(interval)
    setups = []
    for machine in sorted(machines):
        indices = [i for i, (_, op) in enumerate(jobs) if op.machine_id == machine]
        if indices:
            empty = model.NewBoolVar(f"empty_{machine}")
            arcs = [(0, 0, empty)]
            model.Add(sum(presence[jobs[i][0].order_id] for i in indices) == 0).OnlyEnforceIf(empty)
            for left in indices:
                node = left + 1
                arcs.extend([(node, node, presence[jobs[left][0].order_id].Not()),
                             (0, node, model.NewBoolVar(f"first_{left}")),
                             (node, 0, model.NewBoolVar(f"last_{left}"))])
                for right in indices:
                    if left == right:
                        continue
                    follow = model.NewBoolVar(f"next_{left}_{right}")
                    arcs.append((node, right + 1, follow))
                    order, op = jobs[right]
                    setup = op.changeover_minutes if jobs[left][0].product_id != order.product_id else 0
                    model.Add(starts[right] >= ends[left] + setup).OnlyEnforceIf(follow)
                    if setup:
                        start = model.NewIntVar(0, horizon, f"setup_{left}_{right}")
                        machines[machine].append(model.NewOptionalIntervalVar(start, setup, starts[right], follow, f"setup_interval_{left}_{right}"))
                        setups.append((follow, setup))
            model.AddCircuit(arcs)
        model.AddNoOverlap(machines[machine])
    for worker in sorted(workers):
        model.AddNoOverlap(workers[worker])
    lateness = []
    for order in sorted(factory.orders, key=lambda o: o.order_id):
        late = model.NewIntVar(0, horizon, f"late_{order.order_id}")
        model.AddMaxEquality(late, [0, previous[order.order_id] - order.due_at])
        lateness.append(late)
    delivery_weight, setup_weight = {"Balanced": (40, 15), "Delivery First": (70, 5), "Cost First": (25, 30)}[profile]
    bound = (len(jobs) + 1) * horizon * 200
    model.Minimize(-sum(presence.values()) * bound + sum(lateness) * delivery_weight
                   + sum(lit * minutes for lit, minutes in setups) * setup_weight + sum(ends))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    solver.parameters.max_deterministic_time = 0.2
    status = solver.Solve(model)
    output, unscheduled = [], []
    for i, (order, op) in enumerate(jobs):
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE) or not solver.Value(presence[order.order_id]):
            reason = "MATERIAL_SHORTAGE" if reservations[order.order_id]["status"] == "SHORTAGE" else "NO_FEASIBLE_PLAN"
            unscheduled.append({"order_id": order.order_id, "operation_no": op.operation_no, "reason": reason})
        else:
            output.append({"order_id": order.order_id, "operation_no": op.operation_no,
                           "machine_id": op.machine_id, "worker_id": op.worker_id,
                           "start": solver.Value(starts[i]), "end": solver.Value(ends[i]),
                           "duration": op.duration_minutes, "material_id": op.material_id, "material_qty": op.material_qty})
    output.sort(key=lambda r: (r["start"], r["machine_id"], r["order_id"], r["operation_no"]))
    violations = validate_plan(output, factory, unscheduled)
    completion = {r["order_id"]: max(x["end"] for x in output if x["order_id"] == r["order_id"]) for r in output}
    on_time = sum(o.order_id in completion and completion[o.order_id] <= o.due_at for o in factory.orders)
    late_orders = [o.order_id for o in factory.orders if completion.get(o.order_id, 0) > o.due_at]
    changes = []
    lookup = {(o.order_id, op.operation_no): (o, op) for o, op in jobs}
    for machine in sorted(machines):
        sequence = [r for r in output if r["machine_id"] == machine]
        for a, b in zip(sequence, sequence[1:]):
            left, right = lookup[(a["order_id"], a["operation_no"])], lookup[(b["order_id"], b["operation_no"])]
            if left[0].product_id != right[0].product_id:
                changes.append(right[1].changeover_minutes)
    secondary = sum(is_secondary(factory, lookup[(r["order_id"], r["operation_no"])][1]) for r in output)
    kpis = {"eligible_orders": len(factory.orders), "on_time_orders": on_time,
            "on_time_rate": on_time / len(factory.orders) if factory.orders else 0,
            "eligible_order_coverage_rate": len(completion) / len(factory.orders) if factory.orders else 0,
            "late_orders": late_orders, "total_tardiness_min": sum(max(0, completion.get(o.order_id, 0) - o.due_at) for o in factory.orders),
            "overtime_hours": 0, "changeover_count": len(changes), "total_changeover_min": sum(changes),
            "unscheduled_operations": len(unscheduled), "secondary_skill_assignment_count": secondary}
    return SchedulePlan(profile, output, unscheduled, kpis, violations, reservations,
                        ["publish_plan"] + (["assign_qualified_secondary_skill"] if secondary else []), solver.StatusName(status))


def qualified(factory, op):
    if op.worker_id is None:
        return True
    worker = next((w for w in factory.workers if w["worker_id"] == op.worker_id), None)
    if worker is None:
        return False
    if not op.required_skill:
        return True
    return any(isinstance(s, dict) and s.get("skill_id") == op.required_skill and s.get("proficiency_level", 0) >= 2
               and isinstance(s.get("is_primary"), bool) for s in worker.get("skills", []))


def is_secondary(factory, op):
    return any(isinstance(s, dict) and s.get("skill_id") == op.required_skill and s.get("is_primary") is False
               for w in factory.workers if w["worker_id"] == op.worker_id for s in w.get("skills", []))


def validate_plan(operations, factory, unscheduled=None):
    violations = []
    def issue(kind, oid):
        violations.append({"code": "VALIDATION_FAILED", "type": kind, "order_id": oid})
    known = {(o.order_id, op.operation_no): (o, op) for o in factory.orders for op in o.operations}
    reservations, seen = reserve_materials(factory), set()
    for row in operations:
        key = (row["order_id"], row["operation_no"])
        if key in seen:
            issue("duplicate_operation", key[0])
        seen.add(key)
        if key not in known:
            issue("unknown_operation", key[0])
            continue
        order, op = known[key]
        if row["machine_id"] != op.machine_id or row.get("worker_id") != op.worker_id or row["end"] - row["start"] != op.duration_minutes:
            issue("assignment_mismatch", key[0])
        if row.get("material_id") != op.material_id or row.get("material_qty", 0) != op.material_qty:
            issue("material_mismatch", key[0])
        if not qualified(factory, op):
            issue("worker_skill", key[0])
        if not any(a <= row["start"] and row["end"] <= b for a, b in windows(factory, op, factory.horizon)):
            issue("shift_coverage", key[0])
        reservation = reservations[key[0]]
        if reservation["status"] == "SHORTAGE" or row["start"] < max(op.release_at, reservation["ready_at"] or 0):
            issue("material_availability", key[0])
        for block in factory.maintenance:
            if ((block.get("machine_id") == row["machine_id"] or (row.get("worker_id") and block.get("worker_id") == row["worker_id"]))
                    and row["start"] < block["end"] and block["start"] < row["end"]):
                issue("resource_unavailable", key[0])
    for field in ("machine_id", "worker_id"):
        groups = defaultdict(list)
        for row in operations:
            if row.get(field):
                groups[row[field]].append(row)
        for rows in groups.values():
            rows.sort(key=lambda r: (r["start"], r["end"]))
            for i, row in enumerate(rows):
                if any(old["end"] > row["start"] for old in rows[:i]):
                    issue("machine_conflict" if field == "machine_id" else "worker_conflict", row["order_id"])
            if field == "machine_id":
                for a, b in zip(rows, rows[1:]):
                    left, right = known.get((a["order_id"], a["operation_no"])), known.get((b["order_id"], b["operation_no"]))
                    if left and right and left[0].product_id != right[0].product_id:
                        start = b["start"] - right[1].changeover_minutes
                        if start < a["end"] or any(m.get("machine_id") == b["machine_id"] and start < m["end"] and m["start"] < b["start"] for m in factory.maintenance):
                            issue("changeover_conflict", b["order_id"])
    for order in factory.orders:
        rows = sorted((r for r in operations if r["order_id"] == order.order_id), key=lambda r: r["operation_no"])
        if rows and len(rows) != len(order.operations):
            issue("partial_routing", order.order_id)
        for a, b in zip(rows, rows[1:]):
            if a["end"] > b["start"]:
                issue("precedence_conflict", order.order_id)
    omitted = {(r["order_id"], r["operation_no"]) for r in (unscheduled or [])}
    for key in known.keys() - seen - omitted:
        issue("missing_operation", key[0])
    if omitted & seen or omitted - known.keys():
        issue("unscheduled_mismatch", "")
    return violations
