"""Compatibility facade for the V1.8 adapter and independent validator.

The previous implementation manufactured empty calendar references and claimed
thirteen checks after evaluating only two. All semantic work now delegates to
the separated generator and independent validator modules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .independent_validator import hard_constraint_violations, recompute_kpis
from .v18_adapter import build_plan_content


SG = timezone(timedelta(hours=8))
HC = tuple(f"HC-{number:03d}" for number in range(1, 14))


def minute_timestamp(origin: str, minute: int) -> str:
    value = datetime.fromisoformat(origin.replace("Z", "+00:00")) + timedelta(minutes=int(minute))
    return value.astimezone(SG).isoformat(timespec="seconds")


def to_plan_content(candidate: dict, factory_state: dict, plan_id: str, plan_version: int) -> dict:
    return build_plan_content(candidate, factory_state, plan_id, plan_version)


def hard_constraint_report(content: dict, factory_state: dict) -> dict:
    violations = hard_constraint_violations(content, factory_state)
    return {
        "is_feasible": not violations and not content["unscheduled_operations"],
        "hard_violations": violations,
        "checked_constraints": list(HC),
    }


__all__ = [
    "HC",
    "hard_constraint_report",
    "minute_timestamp",
    "recompute_kpis",
    "to_plan_content",
]
