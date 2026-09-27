"""Offline orchestration helpers backed exclusively by deterministic factory tools."""
from dataclasses import asdict

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import build_candidates, validate_plan


def validate_input(payload):
    if not isinstance(payload.get("request"), str) or not payload["request"].strip():
        raise ValueError("request must be non-empty")
    factory_from_dict(payload.get("factory_data"))
    return {"input_valid": True}


def generate_candidates(payload):
    factory = factory_from_dict(payload["factory_data"])
    return {"candidates": [asdict(p) for p in build_candidates(factory)]}


def validate_candidates(payload):
    factory = factory_from_dict(payload["factory_data"])
    candidates = []
    for candidate in payload["candidates"]:
        violations = validate_plan(candidate["operations"], factory, candidate["unscheduled_operations"])
        candidates.append({**candidate, "violations": violations})
    return {"validated_candidates": candidates}


def compare_candidates(payload):
    feasible = [c for c in payload["validated_candidates"] if c["operations"] and not c["violations"]]
    best = max(feasible, key=lambda c: (c["kpis"]["eligible_order_coverage_rate"], c["kpis"]["on_time_rate"],
                                       -c["kpis"]["total_tardiness_min"], -c["kpis"]["total_changeover_min"])) if feasible else None
    if best is None:
        return {"recommendation": None, "approval_required": False, "explanation": "No executable plan; check unscheduled operations and material reservations."}
    kpis = best["kpis"]
    explanation = (f"Recommended {best['profile']}: on-time rate {kpis['on_time_rate']:.1%}, order coverage {kpis['eligible_order_coverage_rate']:.1%}, "
                   f"tardiness {kpis['total_tardiness_min']} min, unscheduled operations {kpis['unscheduled_operations']}, "
                   f"changeovers {kpis['changeover_count']}. Approval is required before publication.")
    return {"recommendation": best["profile"], "approval_required": True, "explanation": explanation}


def demo_registry():
    from .orchestrator import ToolRegistry
    registry = ToolRegistry()
    for name, function in (("validate_input", validate_input), ("generate_candidates", generate_candidates),
                           ("validate_candidates", validate_candidates), ("compare_candidates", compare_candidates)):
        registry.register(name, function)
    return registry
