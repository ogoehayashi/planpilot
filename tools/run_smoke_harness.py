"""Run deterministic component smoke checks without claiming EVAL acceptance."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.lots import split_lots
from planpilot.domain.planning import PROFILES, build_candidates
from planpilot.engine import aggregate_approval_status, escalate, overtime_minutes, stability
from planpilot.workflow.events import EventWorkflow


CONTRACT = ROOT / "contract" / "planpilot_agent_contract_v1.8.json"
FIXTURE = ROOT / "data" / "factory_demo_v18.json"
OUT = ROOT / "tests" / "evidence" / "smoke-harness"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_and_events() -> tuple[dict, dict[str, dict]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "dataset_generator", ROOT / "tools" / "generate_compliant_dataset.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("dataset generator cannot be loaded")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    events = {event["event_id"]: event for event in generator.rows(data)["Events"]}
    return data, events


def _candidate_check(data: dict, _events: dict) -> dict:
    first = build_candidates(factory_from_dict(data))
    second = build_candidates(factory_from_dict(data))
    if tuple(plan.profile for plan in first) != PROFILES:
        raise AssertionError("candidate profiles differ from the closed profile set")
    if [asdict(plan) for plan in first] != [asdict(plan) for plan in second]:
        raise AssertionError("candidate generation is not deterministic on the compact fixture")
    return {"candidate_count": len(first), "profiles": list(PROFILES)}


def _material_check(data: dict, _events: dict) -> dict:
    candidates = build_candidates(factory_from_dict(data))
    ready = 0
    for plan in candidates:
        for reservation in plan.material_reservations.values():
            if reservation["status"] == "READY":
                expected = max((row["available_at"] for row in reservation["allocations"]), default=0)
                if reservation["ready_at"] != expected:
                    raise AssertionError("material ready time does not match allocated batches")
                ready += 1
    return {"ready_reservations_checked": ready}


def _injection_check(data: dict, events: dict) -> dict:
    result = EventWorkflow(data).replan(events["EVT-005"])
    security = result.get("security_event") or {}
    if result.get("state") != "BLOCKED" or len(security.get("event_hash", "")) != 64:
        raise AssertionError("injection-shaped event was not blocked and recorded")
    return {"state": result["state"], "security_event_recorded": True}


def _approval_check(_data: dict, _events: dict) -> dict:
    observed = {
        "pending": aggregate_approval_status(["APPROVED", "PENDING"]),
        "expired": aggregate_approval_status(["EXPIRED"]),
        "invalidated": aggregate_approval_status(["APPROVED"], True),
    }
    if observed != {"pending": "PENDING", "expired": "EXPIRED", "invalidated": "INVALIDATED"}:
        raise AssertionError("approval precedence utility returned an unexpected result")
    return observed


def _fallback_check(_data: dict, _events: dict) -> dict:
    def fail():
        raise RuntimeError("synthetic primary failure")

    result = escalate(fail, lambda: {"candidate": "fallback"})
    if result.provenance != "HEURISTIC_FALLBACK" or result.attempted_rungs != ("CP-SAT", "HEURISTIC_FALLBACK"):
        raise AssertionError("fallback provenance is incomplete")
    return {"provenance": result.provenance, "attempted_rungs": list(result.attempted_rungs)}


def _lot_check(_data: dict, _events: dict) -> dict:
    lots = split_lots("smoke-order", 25, 10)
    if [lot.quantity for lot in lots] != [10, 10, 5] or sum(lot.quantity for lot in lots) != 25:
        raise AssertionError("fixed lot split does not reconcile")
    return {"quantities": [lot.quantity for lot in lots]}


def _stability_check(_data: dict, _events: dict) -> dict:
    reference = [{"order_id": "old", "operation_no": 1}]
    current = [*reference, {"order_id": "new", "operation_no": 1}]
    value = stability(reference, current)
    if value != 0.5:
        raise AssertionError("union-denominator stability check failed")
    return {"stability": value}


def _overtime_check(_data: dict, _events: dict) -> dict:
    value = overtime_minutes(0, 90, [(0, 60)], [(60, 120)])
    if value != 30:
        raise AssertionError("overtime overlap utility returned an unexpected value")
    return {"overtime_minutes": value}


CHECKS: tuple[tuple[str, tuple[str, ...], Callable[[dict, dict], dict]], ...] = (
    ("candidate_determinism", ("EVAL-009",), _candidate_check),
    ("fifo_ready_time", ("EVAL-024",), _material_check),
    ("injection_boundary", ("EVAL-006",), _injection_check),
    ("approval_precedence", ("EVAL-010", "EVAL-014", "EVAL-016"), _approval_check),
    ("generation_fallback", ("EVAL-019",), _fallback_check),
    ("fixed_lot_utility", ("EVAL-021",), _lot_check),
    ("stability_utility", ("EVAL-022", "EVAL-026"), _stability_check),
    ("overtime_utility", ("EVAL-020",), _overtime_check),
)


def run(output: Path = OUT) -> dict:
    data, events = _fixture_and_events()
    results = []
    for check_id, related, check in CHECKS:
        try:
            details = check(data, events)
            status, reason = "pass", "component assertion executed"
        except Exception as exc:
            details = {}
            status, reason = "fail", f"{type(exc).__name__}: {exc}"
        results.append(
            {
                "check_id": check_id,
                "status": status,
                "reason": reason,
                "details": details,
                "related_contract_cases": list(related),
            }
        )
    passed = sum(result["status"] == "pass" for result in results)
    evidence = {
        "evidence_version": 1,
        "evidence_kind": "component_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_sha256": _sha256(CONTRACT),
        "harness_sha256": _sha256(Path(__file__)),
        "fixture": str(FIXTURE.relative_to(ROOT)).replace("\\", "/"),
        "fixture_sha256": _sha256(FIXTURE),
        "python": sys.version,
        "platform": platform.platform(),
        "check_count": len(results),
        "smoke_passed": passed,
        "smoke_failed": len(results) - passed,
        "acceptance_claimed": False,
        "runtime_evaluation": "PENDING_UNTIL_EVAL_001_TO_030_EXECUTE",
        "scope_statement": (
            "These are compact-fixture component smoke checks only. Related contract case IDs "
            "are traceability hints, not executed acceptance cases, and no result here may be "
            "reported as an EVAL PASS."
        ),
        "results": results,
    }
    output.mkdir(parents=True, exist_ok=True)
    target = output / "EVIDENCE.json"
    temporary = output / "EVIDENCE.json.tmp"
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args(argv)
    result = run(args.output)
    print(f"component smoke evidence: {args.output}")
    print(
        f"checks={result['check_count']} smoke_passed={result['smoke_passed']} "
        f"smoke_failed={result['smoke_failed']}"
    )
    print("SCOPE: component smoke only; formal acceptance remains pending.")
    return 0 if result["smoke_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
