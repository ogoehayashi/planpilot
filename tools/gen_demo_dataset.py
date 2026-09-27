"""Generate the shipped demo dataset: ``data/factory_demo_case.json``.

Why this dataset is shaped the way it is
----------------------------------------
The three plan profiles (Balanced / Delivery First / Cost First) can only return
*different* plans if the factory state actually offers them something to trade
off.  The contract fixes the order of the objective:

    Tier 0  unscheduled operations      (shared by every profile)
    Tier 1  late orders, then tardiness (shared by every profile)
    Tier 2  secondary-skill assignments (shared by every profile)
    Tier 3  profile-weighted delivery / overtime / changeover / stability

So tiers 0-2 are settled *before* the profile weights are consulted.  The earlier
8-order dataset removed every lever tier 3 could pull:

  * no OVERTIME shift windows with ``overtime_cap_hours = 0``
        -> the overtime term was identically zero for all three profiles;
  * ``changeover_reference_min = 60`` while real plans total ~190-665 minutes
        -> ``capped_setup = min(total, 60)`` saturated, so the changeover term
           became a constant and stopped discriminating;
  * ``priority`` was perfectly anti-correlated with ``due_at``
        -> "priority first" and "due date first" produced the same sequence.

With no free variable left in tier 3, every profile minimised the same constant
expression and the three plans came back byte-identical — at *any* solver budget.
This generator restores all three levers:

  1. OVERTIME windows for machines and workers, with a real cap and a real daily
     maximum, so "pay overtime to protect the date" is a genuine option.
  2. ``changeover_reference_min`` computed the way the contract defines it (the
     sum of the maximum applicable changeover penalty per eligible production
     operation), so ``capped_setup`` never saturates on a realistic plan.
  3. Priorities decoupled from due dates, so an urgent order and a near-due order
     are not always the same order.

Run:  python tools/gen_demo_dataset.py
"""
from __future__ import annotations

import json
import pathlib

DAY, SHIFT_MIN, HORIZON = 1440, 480, 1440 * 5
OVERTIME_MIN = 240                      # extra window after each regular shift
OUT = pathlib.Path(__file__).resolve().parents[1] / "data" / "factory_demo_case.json"

MACHINES = [("CNC-01", "CNC"), ("MILL-01", "MILL"), ("GRIND-01", "GRIND"), ("QC-01", "QC")]
WORKERS = [
    ("W-01", "Ari", [("CNC", 5, True)]),
    ("W-02", "Bao", [("MILL", 5, True)]),
    ("W-03", "Chen", [("GRIND", 5, True)]),
    ("W-04", "Divya", [("QC", 5, True)]),
]

# product -> (max_lot_size, material, per_unit, routing)
PRODUCTS = {
    "BRACKET-A": (100, "AL-6061", 1, [("CNC-01", "W-01", "CNC", 120, "PRODUCTION"),
                                      ("MILL-01", "W-02", "MILL", 90, "PRODUCTION"),
                                      ("QC-01", "W-04", "QC", 30, "INSPECTION")]),
    "SHAFT-B":   (100, "SS-304", 1, [("CNC-01", "W-01", "CNC", 150, "PRODUCTION"),
                                      ("GRIND-01", "W-03", "GRIND", 120, "PRODUCTION"),
                                      ("QC-01", "W-04", "QC", 30, "INSPECTION")]),
    "HOUSING-C": (100, "AL-6061", 2, [("CNC-01", "W-01", "CNC", 200, "PRODUCTION"),
                                      ("MILL-01", "W-02", "MILL", 120, "PRODUCTION"),
                                      ("GRIND-01", "W-03", "GRIND", 90, "PRODUCTION"),
                                      ("QC-01", "W-04", "QC", 40, "INSPECTION")]),
}

# order_id, product, qty, due day, priority
#
# Priority is deliberately NOT the same ordering as the due date: ORD-4003 is
# promised on day 1 but carries low priority, while ORD-4002 is high priority but
# promised a day later.  Delivery First chases priority, Balanced chases the
# promise date, and the two therefore schedule a different order first.
ORDERS = [
    ("ORD-4001", "BRACKET-A", 40, 1, 8),
    ("ORD-4002", "SHAFT-B",   30, 2, 7),
    ("ORD-4003", "HOUSING-C", 25, 1, 3),
    ("ORD-4004", "BRACKET-A", 50, 2, 6),
    ("ORD-4005", "SHAFT-B",   45, 3, 5),
    ("ORD-4006", "BRACKET-A", 30, 3, 2),
    ("ORD-4007", "HOUSING-C", 20, 4, 4),
    ("ORD-4008", "SHAFT-B",   35, 5, 1),
]

INVENTORY = {
    # Material is sufficient and on hand from minute 0: the contract rejects any
    # plan that leaves an eligible operation unscheduled, so lateness must come
    # from promises and capacity, never from a shortage.
    "SS-304": [("SS-ONHAND-001", 400, 0, "ON_HAND")],
    "AL-6061": [("AL-ONHAND-001", 500, 0, "ON_HAND")],
}

OVERTIME_CAP_HOURS = 16
MAX_OVERTIME_MIN_PER_WORKER_PER_DAY = OVERTIME_MIN


def day(n: int) -> str:
    return "2026-09-%02d" % (14 + n)


def build() -> dict:
    orders = []
    for oid, product, qty, due_day, prio in ORDERS:
        _lot, material, per_unit, routing = PRODUCTS[product]
        ops = []
        for i, (machine, worker, skill, duration, otype) in enumerate(routing, start=1):
            op = {"operation_no": i, "operation_type": otype, "machine_id": machine,
                  "worker_id": worker, "duration": duration, "required_skill": skill}
            if i == 1:
                op["material_id"] = material
                op["material_qty"] = qty * per_unit
            ops.append(op)
        orders.append({"order_id": oid, "product_id": product, "quantity": qty,
                       "due_date": day(due_day), "due_at": due_day * DAY,
                       "priority": prio, "operations": ops})

    shifts = []
    for mid, _ in MACHINES:
        for d in range(5):
            shifts.append({"calendar_window_id": "CAL-M-%s-%d" % (mid, d), "window_type": "REGULAR",
                           "overtime_allowed": False, "start": d * DAY, "end": d * DAY + SHIFT_MIN,
                           "machine_id": mid})
            shifts.append({"calendar_window_id": "CAL-M-%s-%d-OT" % (mid, d), "window_type": "OVERTIME",
                           "overtime_allowed": True, "start": d * DAY + SHIFT_MIN,
                           "end": d * DAY + SHIFT_MIN + OVERTIME_MIN, "machine_id": mid})
    for wid, _, _ in WORKERS:
        for d in range(5):
            shifts.append({"calendar_window_id": "CAL-W-%s-%d" % (wid, d), "window_type": "REGULAR",
                           "overtime_allowed": False, "start": d * DAY, "end": d * DAY + SHIFT_MIN,
                           "worker_id": wid})
            shifts.append({"calendar_window_id": "CAL-W-%s-%d-OT" % (wid, d), "window_type": "OVERTIME",
                           "overtime_allowed": True, "start": d * DAY + SHIFT_MIN,
                           "end": d * DAY + SHIFT_MIN + OVERTIME_MIN, "worker_id": wid})

    changeovers = []
    for mid, _ in MACHINES:
        for a in PRODUCTS:
            for b in PRODUCTS:
                if a == b:
                    changeovers.append({"machine_id": mid, "from_product_id": a,
                                        "to_product_id": b, "minutes": 0})
                    continue
                both = {x[0] for x in PRODUCTS[a][3]} & {x[0] for x in PRODUCTS[b][3]}
                if mid not in both:
                    continue
                # CNC is the constrained resource and carries the heaviest setup, so
                # avoiding a changeover there is a real cost decision; the profile
                # weights then have something to disagree about.
                base = 90 if mid == "CNC-01" else 50
                changeovers.append({"machine_id": mid, "from_product_id": a,
                                    "to_product_id": b, "minutes": base})

    state = {
        "planning_start": "2026-09-14T00:00:00+08:00",
        "horizon": HORIZON,
        "products": [{"product_id": pid, "max_lot_size": v[0], "requires_material": True,
                      "bom": [{"material_id": v[1], "material_uom": "EA", "quantity_per_unit": v[2]}]}
                     for pid, v in PRODUCTS.items()],
        "orders": orders,
        "inventory": {m: {"material_uom": "EA",
                          "batches": [{"batch_id": b, "quantity": q, "available_at": a,
                                       "source_type": s} for b, q, a, s in rows]}
                      for m, rows in INVENTORY.items()},
        "machines": [{"machine_id": m, "machine_group_id": g, "capacity_minutes": SHIFT_MIN * 5}
                     for m, g in MACHINES],
        "workers": [{"worker_id": w, "name": n, "available": True,
                     "skills": [{"skill_id": s, "proficiency_level": p, "is_primary": pr}
                                for s, p, pr in sk]} for w, n, sk in WORKERS],
        "shifts": shifts,
        "maintenance": [],
        "changeovers": changeovers,
        "assumptions": {
            "overtime_cap_hours": OVERTIME_CAP_HOURS,
            "changeover_reference_min": changeover_reference(state=None, orders=orders, changeovers=changeovers),
            "stability_drift_min": 30,
            "allow_optional_lot_splitting": False,
            "max_overtime_min_per_worker_per_day": MAX_OVERTIME_MIN_PER_WORKER_PER_DAY,
        },
        "events": [],
    }
    return state


def changeover_reference(state, orders, changeovers) -> int:
    """The contract's documented loose upper bound:

        sum of the maximum applicable changeover penalty for each eligible
        production operation in the planning horizon

    Using it means ``capped_setup = min(total_setup, reference)`` never saturates
    on a realistic plan, so the changeover term keeps discriminating.
    """
    worst = {}
    for row in changeovers:
        key = (row["machine_id"], row["to_product_id"])
        worst[key] = max(worst.get(key, 0), row["minutes"])
    return sum(worst.get((op["machine_id"], order["product_id"]), 0)
               for order in orders for op in order["operations"]
               if op["operation_type"] == "PRODUCTION") or 1


if __name__ == "__main__":
    data = build()
    OUT.write_text(json.dumps(data, indent=1), encoding="utf-8")
    ops = [op for o in data["orders"] for op in o["operations"]]
    print("wrote %s" % OUT)
    print("  orders=%d operations=%d" % (len(data["orders"]), len(ops)))
    print("  shifts=%d (%d OVERTIME)" % (len(data["shifts"]),
                                         sum(1 for s in data["shifts"] if s["window_type"] == "OVERTIME")))
    print("  changeovers=%d (nonzero=%d)" % (len(data["changeovers"]),
                                             sum(1 for c in data["changeovers"] if c["minutes"] > 0)))
    print("  changeover_reference_min=%d" % data["assumptions"]["changeover_reference_min"])
    print("  overtime_cap_hours=%d  max_overtime_min_per_worker_per_day=%d"
          % (data["assumptions"]["overtime_cap_hours"],
             data["assumptions"]["max_overtime_min_per_worker_per_day"]))
    print("  max operation duration=%d (fits one %d-minute merged window)"
          % (max(op["duration"] for op in ops), SHIFT_MIN + OVERTIME_MIN))
