"""Finite capacity planning with immutable FIFO reservations and independent checks."""
from collections import defaultdict
from copy import deepcopy

from ortools.sat.python import cp_model

from .calendar import covered, merged_intervals, overtime_by_day
from .models import SchedulePlan
from .objectives import profile_weights

PROFILES = ("Balanced", "Delivery First", "Cost First")

# Bounded, reproducible search width for the rung-2 deterministic dispatcher: how
# many candidate start slots one operation may be scored against.  Small enough to
# stay fast, wide enough that a profile which dislikes overtime can look past the
# earliest (possibly overtime) slot into the next regular window.
MAX_PLACEMENT_CANDIDATES = 6

# Deterministic solver budget the production entry point (``build_candidates``)
# gives each of the five lexicographic stages.  The contract's Tier 3 — the only
# stage where the profile weights are consulted — cannot be reached at all if the
# solver spends its whole budget proving earlier tiers, so this must be large
# enough for the tiers to settle.  Measured on the shipped demo dataset: tiers 0-2
# settle to proven optimality in ~1.7 s total, tier 3 uses the rest.  ``solve``
# keeps its own conservative 0.2 s default so callers that pin a budget explicitly
# are unaffected.
DEFAULT_DETERMINISTIC_BUDGET = 5.0


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


def build_candidates(factory, references=None, stability_drift_min=0,
                     deterministic_budget=DEFAULT_DETERMINISTIC_BUDGET):
    references = references or {}
    return [solve(factory, profile, reference_starts=references.get(profile),
                  stability_drift_min=stability_drift_min,
                  deterministic_budget=deterministic_budget) for profile in PROFILES]


def _overtime_minute_table(model, start, active, factory, op, horizon, overtime_daily):
    """Exact legacy overtime encoding: a ``horizon + 1`` lookup table per operation.

    Only used for window layouts the compact form cannot express (overlapping
    REGULAR/OVERTIME windows, or a window crossing midnight), where the per-minute
    definition ``overtime = not regular and overtime`` is not a plain overlap.
    """
    by_start = [overtime_by_day(factory.shifts, op.worker_id, minute, minute + op.duration_minutes)
                for minute in range(horizon + 1)]
    raw = model.NewIntVar(0, op.duration_minutes, "raw_overtime")
    model.AddElement(start, [sum(item.values()) for item in by_start], raw)
    charged = model.NewIntVar(0, op.duration_minutes, "overtime")
    model.AddMultiplicationEquality(charged, [raw, active])
    for day in sorted({day for item in by_start for day in item}):
        raw_day = model.NewIntVar(0, op.duration_minutes, "raw_overtime_day")
        model.AddElement(start, [item.get(day, 0) for item in by_start], raw_day)
        charged_day = model.NewIntVar(0, op.duration_minutes, "overtime_day")
        model.AddMultiplicationEquality(charged_day, [raw_day, active])
        overtime_daily[(op.worker_id, day)].append(charged_day)
    return charged


def _overtime_terms(model, start, end, active, factory, op, horizon, overtime_daily):
    """Charged overtime minutes for one optional operation, as a model variable.

    The previous encoding built a ``horizon + 1``-entry table per operation and fed
    it to ``AddElement``.  On a five-day horizon that is 7,201 Python
    ``overtime_by_day`` calls per operation, which measured ~13 s of model
    construction for 26 operations — before the solver had done any work, and long
    enough that the primary solver effectively never ran.

    Overtime is defined by a handful of shift windows, so it is expressed here as
    the exact overlap of the operation with each overtime window: a few
    ``AddMin``/``AddMax`` constraints instead of a 7,201-entry table.  The
    arithmetic is identical to ``overtime_by_day`` whenever a worker's REGULAR and
    OVERTIME windows are disjoint and no window crosses midnight; for any other
    layout the exact per-minute encoding is used instead.
    """
    overtime = sorted(((row["start"], row["end"]) for row in factory.shifts
                       if row.get("worker_id") == op.worker_id
                       and row.get("window_type") == "OVERTIME"),
                      key=lambda item: (item[0], item[1]))
    regular = [(row["start"], row["end"]) for row in factory.shifts
               if row.get("worker_id") == op.worker_id
               and row.get("window_type", "REGULAR") == "REGULAR"]
    overlaps_regular = any(a < regular_end and regular_start < b
                           for regular_start, regular_end in regular for a, b in overtime)
    crosses_midnight = any(a // 1440 != (b - 1) // 1440 for a, b in overtime)
    if overlaps_regular or crosses_midnight:
        return _overtime_minute_table(model, start, active, factory, op, horizon, overtime_daily)
    pieces = []
    for window_start, window_end in overtime:
        lower = model.NewIntVar(0, horizon, "overtime_lower")
        model.AddMaxEquality(lower, [start, window_start])
        upper = model.NewIntVar(0, horizon, "overtime_upper")
        model.AddMinEquality(upper, [end, window_end])
        gap = model.NewIntVar(-horizon, horizon, "overtime_gap")
        model.Add(gap == upper - lower)
        overlap = model.NewIntVar(0, horizon, "overtime_overlap")
        model.AddMaxEquality(overlap, [0, gap])
        charged = model.NewIntVar(0, horizon, "overtime_charged")
        model.AddMultiplicationEquality(charged, [overlap, active])
        overtime_daily[(op.worker_id, window_start // 1440)].append(charged)
        pieces.append(charged)
    total = model.NewIntVar(0, horizon, "overtime_total")
    model.Add(total == sum(pieces))
    return total


def _transition_minutes(factory, machine_id, from_product_id, to_product_id, default):
    if not factory.changeovers:
        return default if from_product_id != to_product_id else 0
    transitions = {
        (row["machine_id"], row["from_product_id"], row["to_product_id"]): row["minutes"]
        for row in factory.changeovers
    }
    key = (machine_id, from_product_id, to_product_id)
    if key not in transitions:
        raise ValueError(f"missing changeover transition {key}")
    return transitions[key]


def solve(factory, profile, *, deterministic_budget=0.2,
          reference_starts=None, stability_drift_min=0):
    if profile not in PROFILES:
        raise ValueError("unknown profile")
    jobs = [(o, op) for o in sorted(factory.orders, key=lambda o: o.order_id)
            for op in sorted(o.operations, key=lambda op: op.operation_no)]
    horizon = factory.horizon
    reservations = reserve_materials(factory)
    model = cp_model.CpModel()
    presence = {o.order_id: model.NewBoolVar(o.order_id) for o in factory.orders}
    starts, ends, machines, workers, previous = [], [], defaultdict(list), defaultdict(list), {}
    overtime_terms = []
    overtime_daily = defaultdict(list)
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
        if op.worker_id and any(
            row.get("worker_id") == op.worker_id and row.get("window_type") == "OVERTIME"
            for row in factory.shifts
        ):
            charged = _overtime_terms(
                model, start, end, active, factory, op, horizon, overtime_daily,
            )
            overtime_terms.append(charged)
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
                    setup = _transition_minutes(
                        factory, machine, jobs[left][0].product_id,
                        order.product_id, op.changeover_minutes,
                    )
                    model.Add(starts[right] >= ends[left] + setup).OnlyEnforceIf(follow)
                    if setup:
                        if factory.shifts:
                            machine_rows = [row for row in factory.shifts if row.get("machine_id") == machine]
                            setup_choices = []
                            for window_no, (window_start, window_end) in enumerate(merged_intervals(machine_rows)):
                                if window_end - window_start < setup:
                                    continue
                                choice = model.NewBoolVar(f"setup_window_{left}_{right}_{window_no}")
                                setup_choices.append(choice)
                                model.Add(starts[right] >= window_start + setup).OnlyEnforceIf(choice)
                                model.Add(starts[right] <= window_end).OnlyEnforceIf(choice)
                            model.Add(sum(setup_choices) == follow)
                        start = model.NewIntVar(0, horizon, f"setup_{left}_{right}")
                        machines[machine].append(model.NewOptionalIntervalVar(start, setup, starts[right], follow, f"setup_interval_{left}_{right}"))
                        setups.append((follow, setup))
            model.AddCircuit(arcs)
        model.AddNoOverlap(machines[machine])
    for worker in sorted(workers):
        model.AddNoOverlap(workers[worker])
    if factory.overtime_cap_min is not None:
        model.Add(sum(overtime_terms) <= factory.overtime_cap_min)
    if factory.max_overtime_min_per_worker_per_day is not None:
        for terms in overtime_daily.values():
            model.Add(sum(terms) <= factory.max_overtime_min_per_worker_per_day)
    lateness = []
    for order in sorted(factory.orders, key=lambda o: o.order_id):
        late = model.NewIntVar(0, horizon, f"late_{order.order_id}")
        model.AddMaxEquality(late, [0, previous[order.order_id] - order.due_at])
        lateness.append(late)
    late_flags = []
    for order, late in zip(sorted(factory.orders, key=lambda o: o.order_id), lateness):
        flag = model.NewBoolVar(f"late_flag_{order.order_id}")
        model.Add(late >= 1).OnlyEnforceIf([flag, presence[order.order_id]])
        model.Add(late == 0).OnlyEnforceIf([flag.Not(), presence[order.order_id]])
        model.Add(flag == 1).OnlyEnforceIf(presence[order.order_id].Not())
        late_flags.append(flag)
    secondary_terms = [presence[order.order_id] for order in factory.orders
                       for op in order.operations if is_secondary(factory, op)]
    reference_starts = reference_starts or {}
    stable_terms = []
    for i, (order, op) in enumerate(jobs):
        key = (order.order_id, op.operation_no)
        if key not in reference_starts:
            continue
        stable = model.NewBoolVar(f"stable_{i}")
        model.Add(stable <= presence[order.order_id])
        model.Add(starts[i] >= reference_starts[key] - stability_drift_min).OnlyEnforceIf(stable)
        model.Add(starts[i] <= reference_starts[key] + stability_drift_min).OnlyEnforceIf(stable)
        stable_terms.append(stable)
    total_setup = sum(lit * minutes for lit, minutes in setups)
    total_overtime = sum(overtime_terms)
    unscheduled_count = sum((1 - presence[order.order_id]) * len(order.operations)
                            for order in factory.orders)
    delivery_weight, overtime_weight, changeover_weight, stability_weight = profile_weights()[profile]
    max_setup = max(
        [row["minutes"] for row in factory.changeovers]
        + [op.changeover_minutes for _, op in jobs],
        default=0,
    )
    total_setup_var = model.NewIntVar(0, len(jobs) * max_setup, "total_setup")
    model.Add(total_setup_var == total_setup)
    capped_setup = model.NewIntVar(0, factory.changeover_reference_min, "capped_setup")
    model.AddMinEquality(capped_setup, [total_setup_var, factory.changeover_reference_min])
    overtime_cap = factory.overtime_cap_min if factory.overtime_cap_min is not None else 960
    capped_overtime = model.NewIntVar(0, max(overtime_cap, 0), "capped_overtime")
    if overtime_cap == 0:
        model.Add(capped_overtime == 0)
    else:
        total_overtime_var = model.NewIntVar(0, len(jobs) * horizon, "total_overtime")
        model.Add(total_overtime_var == total_overtime)
        model.AddMinEquality(capped_overtime, [total_overtime_var, overtime_cap])
    tier3 = (
        round(delivery_weight * 1_000_000 / max(len(factory.orders), 1)) * sum(late_flags)
        + round(overtime_weight * 1_000_000 / max(overtime_cap, 1)) * capped_overtime
        + round(changeover_weight * 1_000_000 / factory.changeover_reference_min) * capped_setup
        - round(stability_weight * 1_000_000 / max(len(reference_starts), 1)) * sum(stable_terms)
    )
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    if deterministic_budget <= 0:
        return _priority_dispatch(factory, profile, reservations, deterministic_budget)
    solver.parameters.max_deterministic_time = deterministic_budget / 5
    stages = (unscheduled_count, sum(late_flags), sum(lateness), sum(secondary_terms), tier3)
    status = cp_model.UNKNOWN
    for index, expression in enumerate(stages):
        model.Minimize(expression)
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return _priority_dispatch(factory, profile, reservations, deterministic_budget)
        if index < len(stages) - 1:
            if status != cp_model.OPTIMAL:
                return _priority_dispatch(factory, profile, reservations, deterministic_budget)
            model.Add(expression == round(solver.ObjectiveValue()))
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
    return _finish_plan(factory, profile, output, unscheduled, reservations,
                        solver.StatusName(status), deterministic_budget)


def _finish_plan(factory, profile, output, unscheduled, reservations, solver_status,
                 deterministic_budget=0.0):
    violations = validate_plan(output, factory, unscheduled)
    completion = {r["order_id"]: max(x["end"] for x in output if x["order_id"] == r["order_id"]) for r in output}
    on_time = sum(o.order_id in completion and completion[o.order_id] <= o.due_at for o in factory.orders)
    late_orders = [o.order_id for o in factory.orders if completion.get(o.order_id, 0) > o.due_at]
    changes = []
    lookup = {(o.order_id, op.operation_no): (o, op) for o in factory.orders for op in o.operations}
    for machine in sorted({row["machine_id"] for row in output}):
        sequence = [r for r in output if r["machine_id"] == machine]
        for a, b in zip(sequence, sequence[1:]):
            left, right = lookup[(a["order_id"], a["operation_no"])], lookup[(b["order_id"], b["operation_no"])]
            changes.append(_transition_minutes(factory, machine, left[0].product_id,
                                               right[0].product_id, right[1].changeover_minutes))
    secondary = sum(is_secondary(factory, lookup[(r["order_id"], r["operation_no"])][1]) for r in output)
    kpis = {"eligible_orders": len(factory.orders), "on_time_orders": on_time,
            "on_time_rate": on_time / len(factory.orders) if factory.orders else 0,
            "eligible_order_coverage_rate": len(completion) / len(factory.orders) if factory.orders else 0,
            "late_orders": late_orders, "total_tardiness_min": sum(max(0, completion.get(o.order_id, 0) - o.due_at) for o in factory.orders),
            "overtime_hours": sum(sum(overtime_by_day(factory.shifts, r["worker_id"], r["start"], r["end"]).values()) for r in output) / 60,
            "changeover_count": sum(change > 0 for change in changes), "total_changeover_min": sum(changes),
            "unscheduled_operations": len(unscheduled), "secondary_skill_assignment_count": secondary}
    return SchedulePlan(profile, output, unscheduled, kpis, violations, reservations,
                        ["publish_plan"] + (["assign_qualified_secondary_skill"] if secondary else []), solver_status,
                        deterministic_budget)


def _dispatch_sequence(profile):
    """Order sequence for the rung-2 dispatcher, per profile.

    ``Delivery First`` and ``Balanced`` both dispatch earliest-promise-date-first,
    because the contract gives every profile the same service-level obligation
    (Tier 1 settles lateness before the profile weights are consulted at all).
    ``Cost First`` instead runs one product family back to back, which is the one
    sequencing choice that changes how much changeover a machine pays.
    """
    if profile == "Cost First":
        return lambda order: (order.product_id, order.due_at, -order.priority, order.order_id)
    return lambda order: (order.due_at, -order.priority, order.order_id)


def _priority_dispatch(factory, profile, reservations, deterministic_budget=0.0):
    """Deterministic whole-order dispatch (rung 2 / HEURISTIC_FALLBACK).

    Orders are dispatched in the contract's service-level order — priority, then
    promise date — so the fallback gives up as little delivery as possible when
    the primary solver cannot run.  The profile is applied where a dispatcher still
    has a genuine choice: the start time selected for each operation is the
    cheapest under the contract's own profile weights over tardiness and overtime
    (_select_start).

    The guard this dispatcher exists to provide: a profile-blind fallback returns
    byte-identical plans for all three profiles at any budget, which is precisely
    the defect that made the three-plan comparison meaningless.  The complete
    tier-3 trade-off (changeover and stability as well as overtime) is expressed
    by the primary solver, which build_candidates now budgets for.
    """
    weights = profile_weights()[profile]
    delivery_weight, overtime_weight = weights[0], weights[1]
    context = _op_context(factory)
    machine_end, machine_product, worker_end = {}, {}, {}
    daily_overtime = defaultdict(int)
    overtime_total = 0
    output, unscheduled = [], []
    for order in sorted(factory.orders, key=_dispatch_sequence(profile)):
        reservation = reservations[order.order_id]
        local_machine_end, local_machine_product = dict(machine_end), dict(machine_product)
        local_worker_end, local_daily = dict(worker_end), dict(daily_overtime)
        local_total, tentative = overtime_total, []
        if reservation["status"] == "READY":
            cursor = 0
            for op in sorted(order.operations, key=lambda item: item.operation_no):
                if not qualified(factory, op):
                    break
                prior_product = local_machine_product.get(op.machine_id)
                setup = 0 if prior_product is None else _transition_minutes(
                    factory, op.machine_id, prior_product, order.product_id,
                    op.changeover_minutes,
                )
                minimum = max(cursor, op.release_at, reservation["ready_at"] or 0,
                              local_machine_end.get(op.machine_id, 0) + setup,
                              local_worker_end.get(op.worker_id, 0))
                assignment = _select_start(
                    factory, op, order, minimum, setup, delivery_weight, overtime_weight,
                    local_total, local_daily, *context[(order.order_id, op.operation_no)],
                )
                if assignment is None:
                    break
                start, end, by_day = assignment
                tentative.append({
                    "order_id": order.order_id, "operation_no": op.operation_no,
                    "machine_id": op.machine_id, "worker_id": op.worker_id,
                    "start": start, "end": end, "duration": op.duration_minutes,
                    "material_id": op.material_id, "material_qty": op.material_qty,
                })
                cursor = end
                local_machine_end[op.machine_id] = end
                local_machine_product[op.machine_id] = order.product_id
                if op.worker_id:
                    local_worker_end[op.worker_id] = end
                local_total += sum(by_day.values())
                for day, minutes in by_day.items():
                    key = (op.worker_id, day)
                    local_daily[key] = local_daily.get(key, 0) + minutes
        if len(tentative) == len(order.operations):
            output.extend(tentative)
            machine_end, machine_product, worker_end = local_machine_end, local_machine_product, local_worker_end
            daily_overtime, overtime_total = defaultdict(int, local_daily), local_total
        else:
            reason = "MATERIAL_SHORTAGE" if reservation["status"] == "SHORTAGE" else "NO_FEASIBLE_PLAN"
            unscheduled.extend({"order_id": order.order_id, "operation_no": op.operation_no,
                                "reason": reason} for op in order.operations)
    output.sort(key=lambda row: (row["start"], row["machine_id"], row["order_id"], row["operation_no"]))
    return _finish_plan(factory, profile, output, unscheduled, reservations, "HEURISTIC_FALLBACK",
                        deterministic_budget)


def _op_context(factory):
    """Pre-computed, start-independent facts for every operation in the state.

    Computing these once per operation (instead of once per candidate start) is
    what keeps the dispatcher fast on a five-day horizon.
    """
    context = {}
    for order in factory.orders:
        for op in order.operations:
            context.setdefault((order.order_id, op.operation_no), (
                windows(factory, op, factory.horizon),
                [row for row in factory.shifts if row.get("machine_id") == op.machine_id],
                [block for block in factory.maintenance
                 if block.get("machine_id") == op.machine_id
                 or (op.worker_id and block.get("worker_id") == op.worker_id)],
                bool(op.worker_id) and any(
                    row.get("window_type") == "OVERTIME" and row.get("worker_id") == op.worker_id
                    for row in factory.shifts),
            ))
    return context


def _placement_candidates(factory, op, minimum):
    """Deterministic candidate start bounds for one operation.

    ``minimum`` is the earliest the operation could start at all.  A profile that
    dislikes overtime also needs to be able to consider the start of a later
    regular window, so those window starts are added as further bounds — capped,
    so the search stays bounded and reproducible.
    """
    bounds = {minimum}
    if any(row.get("window_type") == "OVERTIME" for row in factory.shifts):
        rows = [row for row in factory.shifts
                if op.worker_id is not None and row.get("worker_id") == op.worker_id
                and row.get("window_type", "REGULAR") == "REGULAR"]
        for row in sorted(rows, key=lambda r: (r["start"], r["end"], str(r.get("calendar_window_id", "")))):
            if row["start"] > minimum and row["start"] + op.duration_minutes <= row["end"]:
                bounds.add(row["start"])
                if len(bounds) >= MAX_PLACEMENT_CANDIDATES:
                    break
    return sorted(bounds)


def _earliest_start(factory, op, bound, setup, op_windows, machine_rows, maintenance,
                    overtime_used, daily_overtime, counts_overtime):
    """Earliest feasible start at or after ``bound``, or None."""
    for start in range(bound, factory.horizon - op.duration_minutes + 1):
        end = start + op.duration_minutes
        if not any(a <= start and end <= b for a, b in op_windows):
            continue
        setup_start = start - setup
        if setup and factory.shifts and not covered(machine_rows, setup_start, start):
            continue
        if any(setup_start < block["end"] and block["start"] < end for block in maintenance):
            continue
        by_day = overtime_by_day(factory.shifts, op.worker_id, start, end) if counts_overtime else {}
        if factory.overtime_cap_min is not None and overtime_used + sum(by_day.values()) > factory.overtime_cap_min:
            continue
        if factory.max_overtime_min_per_worker_per_day is not None and any(
            daily_overtime.get((op.worker_id, day), 0) + minutes > factory.max_overtime_min_per_worker_per_day
            for day, minutes in by_day.items()
        ):
            continue
        return start, end, by_day
    return None


def _select_start(factory, op, order, minimum, setup, delivery_weight, overtime_weight,
                  overtime_used, daily_overtime, op_windows, machine_rows, maintenance,
                  counts_overtime):
    """Cheapest profile-weighted feasible start for one operation.

    Cost is the contract's own profile trade-off between tardiness and overtime.
    The operation's changeover is fixed by the machine's previous product, so it
    cannot separate two start times for the *same* operation; changeover is
    expressed by the dispatch sequence instead.
    """
    best = None
    for bound in _placement_candidates(factory, op, minimum):
        found = _earliest_start(factory, op, bound, setup, op_windows, machine_rows,
                                maintenance, overtime_used, daily_overtime, counts_overtime)
        if found is None:
            continue
        start, end, by_day = found
        cost = (delivery_weight * max(0, end - order.due_at)
                + overtime_weight * sum(by_day.values()))
        key = (cost, start, end)
        if best is None or key < best[0]:
            best = (key, found)
    return None if best is None else best[1]




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
                    if left and right:
                        setup = _transition_minutes(factory, b["machine_id"],
                                                    left[0].product_id, right[0].product_id,
                                                    right[1].changeover_minutes)
                        start = b["start"] - setup
                        machine_rows = [window for window in factory.shifts if window.get("machine_id") == b["machine_id"]]
                        if (start < a["end"] or
                            (setup and factory.shifts and not covered(machine_rows, start, b["start"])) or
                            any(m.get("machine_id") == b["machine_id"] and start < m["end"] and m["start"] < b["start"] for m in factory.maintenance)):
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
