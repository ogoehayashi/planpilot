"""Compatibility entry points delegate to the single deterministic scheduler."""
from dataclasses import asdict

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import solve, validate_plan


def optimize_schedule(data, objective="Balanced"):
    return asdict(solve(factory_from_dict(data), objective))


def schedule_orders(data):
    return optimize_schedule(data)


def validate_schedule(plan, factory_data):
    violations = validate_plan(plan["operations"], factory_from_dict(factory_data), plan.get("unscheduled_operations", []))
    return {"is_feasible": not violations, "hard_violations": violations}
