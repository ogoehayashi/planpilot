"""Compact demo and utility checks only; never formal contract acceptance."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import build_candidates
from planpilot.domain.lots import split_lots
from planpilot.engine import aggregate_approval_status, overtime_minutes, stability

FIXTURE = ROOT / "data" / "factory_demo_v18.json"
OUT = ROOT / "tests" / "evidence" / "compact-smoke"


def _candidate_checks():
    factory = factory_from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))
    candidates = build_candidates(factory)
    assert len(candidates) == 3
    assert all(plan.operations for plan in candidates)
    assert candidates == build_candidates(factory)
    assert all(not plan.violations for plan in candidates)
    assert all(0 <= plan.kpis["eligible_order_coverage_rate"] <= 1 for plan in candidates)
    assert all(plan.material_reservations for plan in candidates)
    assert all("publish_plan" in plan.required_actions for plan in candidates)


def _approval_aggregation():
    assert aggregate_approval_status(["APPROVED", "PENDING"]) == "PENDING"
    assert aggregate_approval_status(["EXPIRED"]) == "EXPIRED"
    assert aggregate_approval_status(["APPROVED"], True) == "INVALIDATED"


def _overtime_overlap():
    assert overtime_minutes(0, 90, [(0, 60)], [(60, 120)]) == 30


def _lot_repeatability():
    lots = split_lots("O", 25, 10)
    assert lots and lots == split_lots("O", 25, 10)


def _stability_changes():
    assert stability([{"order_id": "O", "operation_no": 1}],
                     [{"order_id": "O", "operation_no": 1}, {"order_id": "N", "operation_no": 1}]) < 1
    assert stability([{"order_id": "O", "operation_no": 1, "lot_quantity": 10}],
                     [{"order_id": "O", "operation_no": 1, "lot_quantity": 11}]) == 0


def _optional_splitting_rejected():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    try:
        factory_from_dict({**fixture, "allow_optional_lot_splitting": True})
    except ValueError:
        return
    raise AssertionError("compact importer accepted optional splitting")


CHECKS = (
    ("compact_candidates_repeatability_and_local_guards", _candidate_checks),
    ("approval_status_utility", _approval_aggregation),
    ("overtime_overlap_utility", _overtime_overlap),
    ("lot_utility_repeatability", _lot_repeatability),
    ("stability_utility_changes", _stability_changes),
    ("compact_importer_rejects_optional_splitting", _optional_splitting_rejected),
)


def run(output: Path = OUT):
    if not __debug__:
        raise RuntimeError("Smoke assertions require Python without -O/PYTHONOPTIMIZE")
    output = Path(output)
    formal = ROOT / "tests" / "evidence" / "runtime-eval"
    if output.resolve().is_relative_to(formal.resolve()):
        raise ValueError("Smoke output must be separate from formal evidence")
    results = []
    for name, check in CHECKS:
        start = perf_counter()
        try:
            check()
        except Exception as exc:
            status, reason = "FAIL", f"{type(exc).__name__}: {exc}"
        else:
            status, reason = "PASS", "Named compact smoke assertions completed"
        results.append({"check": name, "status": status, "reason": reason,
                        "elapsed_seconds": perf_counter() - start})
    evidence = {
        "evidence_kind": "compact_smoke", "formal_acceptance": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version, "platform": platform.platform(),
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope_statement": "Compact demo/utility assertions only; no full V1.8 validation, authority integration, trace or approval/publication acceptance is established.",
        "check_count": len(results),
        "passed": sum(r["status"] == "PASS" for r in results),
        "failed": sum(r["status"] == "FAIL" for r in results), "results": results,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "SMOKE_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args(argv)
    result = run(args.output)
    print(f"compact smoke only: checks={result['check_count']} passed={result['passed']} failed={result['failed']}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
