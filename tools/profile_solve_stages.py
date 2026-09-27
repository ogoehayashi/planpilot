"""Per-stage profiling of the lexicographic solve: where does the budget go, and
which stage fails to be proven optimal?  Monkeypatches the CP-SAT solver so no
production code is modified.
"""
import json, pathlib, sys, time

ROOT = pathlib.Path(r"C:\Users\Asus\PlanPilot_repo")
sys.path.insert(0, str(ROOT / "src"))

from ortools.sat.python import cp_model                        # noqa: E402
from planpilot.domain.importer import factory_from_dict        # noqa: E402
from planpilot.runtime_planning import _solver_state           # noqa: E402
import planpilot.domain.planning as planning                  # noqa: E402

STATUS = {cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE",
          cp_model.INFEASIBLE: "INFEASIBLE", cp_model.UNKNOWN: "UNKNOWN",
          cp_model.MODEL_INVALID: "INVALID"}

original = cp_model.CpSolver.Solve


def recording_solve(self, model, *args, **kwargs):
    t0 = time.perf_counter()
    status = original(self, model, *args, **kwargs)
    RECORD.append((STATUS.get(status, status), round(time.perf_counter() - t0, 3),
                   self.parameters.max_deterministic_time))
    return status


cp_model.CpSolver.Solve = recording_solve

state = json.loads((ROOT / "data/factory_demo_case.json").read_text(encoding="utf-8"))
expanded, _ = _solver_state(state)
factory = factory_from_dict(expanded)

for budget in (float(x) for x in sys.argv[1:] or ["20"]):
    print("=" * 78)
    print("deterministic_budget=%s  (max_deterministic_time per stage = %.2f)"
          % (budget, budget / 5))
    for profile in planning.PROFILES:
        RECORD = []
        t0 = time.perf_counter()
        plan = planning.solve(factory, profile, deterministic_budget=budget)
        total = time.perf_counter() - t0
        k = plan.kpis
        print("  %-14s -> %-18s %.2fs total  OT=%.2f chg=%s/%s late=%s"
              % (profile, plan.solver_status, total, k["overtime_hours"],
                 k["changeover_count"], k["total_changeover_min"], k["late_orders"]))
        labels = ["tier0 unscheduled", "tier1 late count", "tier1b tardiness",
                  "tier2 secondary", "tier3 profile-composite"]
        for label, (status, secs, md) in zip(labels, RECORD):
            print("      %-24s %-9s %6.2fs" % (label, status, secs))
        if len(RECORD) < 5:
            print("      (bailed to fallback after %d stages)" % len(RECORD))
