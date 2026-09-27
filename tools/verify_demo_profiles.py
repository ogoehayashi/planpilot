"""End-to-end proof that the agent actually produces three usable plans.

Runs the REAL production path — ``generate_authoritative_plans`` — against a
temporary authority database, so the independent validator, the approval-impact
derivation and the plan store all run exactly as they do in the deployed app.
A plan that leaves an eligible operation unscheduled is rejected here, which is
the failure mode the live UI surfaces as HTTP 400
"Eligible routing operations remain unscheduled."

  python tools/verify_demo_profiles.py [data/factory_demo_case.json]
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from planpilot.authority import RuntimeAuthority                 # noqa: E402
from planpilot.persistence import Database                       # noqa: E402
from planpilot.runtime_planning import generate_authoritative_plans  # noqa: E402

FIELDS = ("on_time_rate", "on_time_orders", "eligible_orders", "late_orders",
          "total_tardiness_min", "overtime_hours", "changeover_count",
          "total_changeover_min", "unscheduled_operations",
          "secondary_skill_assignment_count")


def main() -> int:
    dataset = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "data/factory_demo_case.json")
    state = json.loads(dataset.read_text(encoding="utf-8"))
    print("dataset: %s" % dataset.relative_to(ROOT))

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(pathlib.Path(tmp) / "authority.db")
        try:
            authority = RuntimeAuthority(db)
            result = generate_authoritative_plans(state, authority)
        finally:
            db.close()

    options = result["plan_options"]
    print("state=%s  stored_plan_count=%d  solver_runtime=%.2fs"
          % (result["state"], result["stored_plan_count"], result["solver_runtime_seconds"]))
    print()

    digests, signatures = set(), set()
    failed = False
    for option in options:
        content = authority.get_plan(option["plan_id"], option["plan_version"])["content"]
        engine = option["engine"]
        kpis = option["kpis"]
        digest = option["plan_digest"]
        digests.add(digest)
        signatures.add(json.dumps({f: (round(kpis[f], 4) if isinstance(kpis[f], float) else kpis[f])
                                   for f in FIELDS}, sort_keys=True))
        requirements = ["%s (%s)" % (r["action"], r["approver_role"])
                        for r in option["approval_requirements"]]
        print("%-14s %s v%d" % (option["profile"], option["plan_id"], option["plan_version"]))
        print("   digest      : %s" % digest)
        print("   engine      : solver=%s status=%s budget=%s"
              % (engine["solver"], engine["solver_status"], engine["deterministic_budget"]))
        print("   kpis        : ot=%.2fh chg=%s/%s on_time=%s/%s unscheduled=%s"
              % (kpis["overtime_hours"], kpis["changeover_count"], kpis["total_changeover_min"],
                 kpis["on_time_orders"], kpis["eligible_orders"], kpis["unscheduled_operations"]))
        print("   approvals   : %s" % (requirements or ["publish_plan"]))
        print("   operations  : %d scheduled / %d unscheduled"
              % (len(content["operations"]), len(content["unscheduled_operations"])))
        print("   infeasible  : %s" % content["infeasible_reason"])
        if engine["deterministic_budget"] != 5.0:
            print("   *** provenance budget mismatch ***")
            failed = True
        if kpis["unscheduled_operations"]:
            print("   *** UNSCHEDULED WORK — the contract rejects this plan ***")
            failed = True
        print()

    print("distinct plan digests     : %d/3" % len(digests))
    print("distinct KPI signatures   : %d/3" % len(signatures))
    print("recommended plan          : %s (%s)"
          % (result["plan_id"], result["content"]["profile"]))

    ok = len(digests) == 3 and len(signatures) == 3 and not failed
    print()
    print("RESULT: %s" % ("PASS - three distinct, independently validated plans"
                          if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
