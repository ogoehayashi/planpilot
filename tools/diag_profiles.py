"""Diagnose why the three plan profiles return identical KPIs.

Runs the REAL production path (runtime_planning._solver_state -> factory_from_dict
-> build_candidates) against a chosen dataset, at several deterministic budgets,
and reports whether the three profiles actually differ.

  python tools/diag_profiles.py data/factory_demo_case.json 0.2 1 5 20 40
"""
import json
import pathlib
import sys
import time
from dataclasses import asdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from planpilot.domain.importer import factory_from_dict          # noqa: E402
from planpilot.domain.planning import (                          # noqa: E402
    DEFAULT_DETERMINISTIC_BUDGET, PROFILES, build_candidates, solve,
)
from planpilot.runtime_planning import _solver_state             # noqa: E402

KPI_FIELDS = (
    "on_time_rate", "eligible_orders", "late_orders", "total_tardiness_min",
    "overtime_hours", "changeover_count", "total_changeover_min",
    "unscheduled_operations", "secondary_skill_assignment_count",
)


def signature(kpis):
    return json.dumps(
        {f: (round(kpis[f], 4) if isinstance(kpis[f], float) else kpis[f]) for f in KPI_FIELDS},
        sort_keys=True,
    )


def main():
    dataset = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "data/factory_demo_case.json")
    budgets = [float(x) for x in sys.argv[2:]] or [0.2, 1.0, 5.0, 20.0, 40.0]

    state = json.loads(dataset.read_text(encoding="utf-8"))
    solver_state, identities = _solver_state(state)
    factory = factory_from_dict(solver_state)

    orders = state["orders"]
    ops = sum(len(o["operations"]) for o in orders)
    print("dataset      : %s" % dataset.name)
    print("orders       : %d orders / %d operations" % (len(orders), ops))
    print("lots after lot-splitting: %d (solver jobs: %d)"
          % (len(identities), sum(len(o['operations']) for o in solver_state['orders'])))
    print("horizon      : %s minutes" % state.get("horizon"))
    print("planning_start: %s" % state.get("planning_start"))
    print()

    for budget in budgets:
        rows, sigs = [], []
        for profile in PROFILES:
            t0 = time.perf_counter()
            plan = solve(factory, profile, deterministic_budget=budget)
            dt = time.perf_counter() - t0
            kpis = plan.kpis
            sigs.append(signature(kpis))
            rows.append(
                "  %-14s %-18s on_time=%s/%s late=%s tard=%s chg=%s/%-4s ot=%s  %.2fs"
                % (profile, plan.solver_status,
                   kpis["on_time_orders"], kpis["eligible_orders"], kpis["late_orders"],
                   kpis["total_tardiness_min"], kpis["changeover_count"],
                   kpis["total_changeover_min"], kpis["overtime_hours"], dt)
            )
        distinct = len(set(sigs))
        flag = "DISTINCT OK" if distinct == len(PROFILES) else "*** IDENTICAL BUG ***"
        print("budget=%-6s -> %d/%d %s" % (budget, distinct, len(PROFILES), flag))
        for row in rows:
            print(row)
        print()

    print("--- PRODUCTION PATH (build_candidates, budget=%s) ---" % DEFAULT_DETERMINISTIC_BUDGET)
    started = time.perf_counter()
    plans = build_candidates(factory)
    elapsed = time.perf_counter() - started
    sigs, statuses = [], []
    for plan in plans:
        kpis = plan.kpis
        sigs.append(signature(kpis))
        statuses.append(plan.solver_status)
        print("  %-14s %-18s on_time=%s/%s late=%s tard=%s chg=%s/%s ot=%s"
              % (plan.profile, plan.solver_status, kpis["on_time_orders"],
                 kpis["eligible_orders"], kpis["late_orders"], kpis["total_tardiness_min"],
                 kpis["changeover_count"], kpis["total_changeover_min"], kpis["overtime_hours"]))
    print("  %d/3 distinct, %.2fs total, statuses=%s"
          % (len(set(sigs)), elapsed, statuses))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
