"""Schema-conformant plan fixtures, derived from the contract.

Fixtures are built against the live contract schemas and validated with
jsonschema before use, so a fixture that drifts from a contract revision fails
loudly at import time instead of silently testing the wrong shape.

Every value here is synthetic. Nothing comes from the mock factory dataset —
this spec has no dataset dependency by design (see design.md §4).
"""

from __future__ import annotations

import copy
import json

# THE PRODUCTION ENTRY POINT. These fixtures used to compile their own
# Draft202012Validator over `_CONTRACT["$defs"]` and keep a second `_SCOPED`
# cache. Two consequences, both bad:
#
# 1. The suite could pass while production failed. A test asserting
#    `not fixtures.is_valid(polluted, "plan_content")` proved the FIXTURE
#    compiler rejected it, which is not the same fact as the store rejecting it.
#    That is precisely the shape of audit finding P0-1 — and
#    src/planpilot/validation/schema.py already claimed, in its module docstring,
#    that this module delegated here. The claim was false until this change.
#
# 2. The two resolvers could pick different FILES. _find_contract() hardcoded
#    planpilot_agent_contract_v1.8.json; production contract_path() selects the
#    highest SEMANTIC version and verifies its sha256. With only v1.8 on disk
#    they agreed, so nothing caught it — but on the day v1.9 lands, the tests
#    would validate against v1.8 while src/ validated against v1.9, and the suite
#    would stay green.
#
# Delegating leaves one resolver, one compiler and one cache, so "the contract
# says X" cannot mean two different things in one repo.
from planpilot.validation import (
    contract_path as _production_contract_path,
    contract_sha256,
    is_valid as _production_is_valid,
    validate as _production_validate,
    validation_issues_for,
    validator_for as _production_validator_for,
)

CONTRACT_PATH = _production_contract_path()
REPO_ROOT = CONTRACT_PATH.parent.parent

_CONTRACT = json.loads(CONTRACT_PATH.read_bytes().decode("utf-8"))


def _validator_for(def_name: str):
    """Validator scoped to one $defs entry — the production one, not a copy."""
    return _production_validator_for(def_name)


def validate(instance: dict, def_name: str) -> None:
    """Validate against $defs/<def_name>, raising on the first problem.

    Raises `planpilot.validation.SchemaValidationError` (a ValueError) rather
    than a bare jsonschema.ValidationError. No test in this repo catches the
    fixtures' exception type — all 25 call sites assert success — so the change
    is invisible to them, and the richer error is what makes a fixture failure
    readable.
    """
    _production_validate(instance, def_name, def_name, None)


def is_valid(instance: dict, def_name: str) -> bool:
    return _production_is_valid(instance, def_name)


def errors_for(instance: dict, def_name: str) -> list[str]:
    """All validation errors as readable strings (for negative tests)."""
    issues = validation_issues_for(instance, def_name, def_name, None)
    return sorted(i.message for i in issues)


def contract() -> dict:
    """The loaded contract, for tests that assert against non-$defs sections
    (e.g. tool_execution_contract.retryability_registry)."""
    return _CONTRACT


def contract_hash() -> str:
    """The sha256 of the contract these fixtures were built from."""
    return contract_sha256(CONTRACT_PATH)



# ---------------------------------------------------------------------------
# Fixed instants. RFC 3339 with +08:00 per the contract timezone.
# Lexicographic order == chronological order because the offset is constant,
# which is what lets sort_operations compare strings without parsing.
# ---------------------------------------------------------------------------
T0 = "2026-09-14T08:00:00+08:00"
T1 = "2026-09-14T10:30:00+08:00"
T2 = "2026-09-14T13:00:00+08:00"
T3 = "2026-09-15T08:00:00+08:00"


def make_operation(
    *,
    order_id: str = "ORD-A",
    product_id: str = "PRD-100",
    lot_no: int = 1,
    lot_quantity: int = 50,
    operation_no: int = 1,
    operation_type: str = "PRODUCTION",
    machine_id: str = "MC-01",
    worker_id: str = "WK-01",
    start_time: str = T0,
    end_time: str = T1,
    calendar_window_ids: tuple[str, ...] = ("SC-MC-01-D1-REG", "SC-WK-01-D1-REG"),
    duration_min: int = 150,
    overtime_min: int = 0,
    changeover_min: int = 15,
    risk_level: str = "Low",
    decision_reason_codes: tuple[str, ...] = ("EARLIEST_DUE_DATE", "PRIMARY_SKILL_MATCH"),
    decision_summary: str = "Assigned to earliest available qualified window.",
) -> dict:
    """One schedule_operation, all 17 required fields, additionalProperties false."""
    return {
        "order_id": order_id,
        "product_id": product_id,
        "lot_no": lot_no,
        "lot_quantity": lot_quantity,
        "operation_no": operation_no,
        "operation_type": operation_type,
        "machine_id": machine_id,
        "worker_id": worker_id,
        "start_time": start_time,
        "end_time": end_time,
        # list, not tuple: JSON has no tuple type and the digest must see a list
        "calendar_window_ids": list(calendar_window_ids),
        "duration_min": duration_min,
        "overtime_min": overtime_min,
        "changeover_min": changeover_min,
        "risk_level": risk_level,
        "decision_reason_codes": list(decision_reason_codes),
        "decision_summary": decision_summary,
    }


def make_reservation(
    *,
    order_id: str = "ORD-A",
    product_id: str = "PRD-100",
    lot_no: int = 1,
    lot_quantity: int = 50,
    priority_rank: int = 1,
    status: str = "READY",
    earliest_material_ready_time: str | None = T0,
    material_id: str = "MAT-STEEL-4140",
) -> dict:
    """One lot_material_reservation with a single ON_HAND allocation line."""
    return {
        "order_id": order_id,
        "product_id": product_id,
        "lot_no": lot_no,
        "lot_quantity": lot_quantity,
        "priority_rank": priority_rank,
        "status": status,
        "earliest_material_ready_time": earliest_material_ready_time,
        "lines": [
            {
                "material_id": material_id,
                "material_uom": "G",
                "required_quantity_base_units": 12500,
                "reserved_quantity_base_units": 12500,
                "allocations": [
                    {
                        "source_type": "ON_HAND",
                        "source_id": "LOT-2026-0911-A",
                        "available_at": T0,
                        "quantity_base_units": 12500,
                    }
                ],
            }
        ],
    }


def make_unscheduled(
    *,
    order_id: str = "ORD-C",
    product_id: str = "PRD-300",
    lot_no: int | None = 1,
    operation_no: int = 2,
    reason_code: str = "MATERIAL_SHORTAGE",
    reason: str = "Required material not ready inside the horizon.",
) -> dict:
    """One unscheduled_operation — certified, never silently omitted."""
    return {
        "order_id": order_id,
        "product_id": product_id,
        "lot_no": lot_no,
        "operation_no": operation_no,
        "reason_code": reason_code,
        "reason": reason,
    }


def make_kpis(
    *,
    on_time_rate: float = 0.8,
    eligible_orders: int = 5,
    on_time_orders: int = 4,
    eligible_order_coverage_rate: float = 1.0,
    late_orders: int = 1,
    total_tardiness_min: int = 95,
    overtime_hours: float = 2.5,
    changeover_count: int = 3,
    total_changeover_min: int = 45,
    schedule_stability: float = 0.9,
    unscheduled_operations: int = 1,
    secondary_skill_assignment_count: int = 0,
) -> dict:
    """All 12 required KPIs.

    `eligible_orders` and `eligible_order_coverage_rate` are the anti-gaming pair:
    an eligible order with any unscheduled required operation is not on time, so
    dropping an order cannot raise on_time_rate.
    """
    return {
        "on_time_rate": on_time_rate,
        "eligible_orders": eligible_orders,
        "on_time_orders": on_time_orders,
        "eligible_order_coverage_rate": eligible_order_coverage_rate,
        "late_orders": late_orders,
        "total_tardiness_min": total_tardiness_min,
        "overtime_hours": overtime_hours,
        "changeover_count": changeover_count,
        "total_changeover_min": total_changeover_min,
        "schedule_stability": schedule_stability,
        "unscheduled_operations": unscheduled_operations,
        "secondary_skill_assignment_count": secondary_skill_assignment_count,
    }


def make_engine(*, solver: str = "CP-SAT", solver_status: str = "OPTIMAL",
                random_seed: int = 42, time_budget_seconds: float = 32.0,
                deterministic_budget: float = 32.0, objective_value: float | None = 1234.5,
                canonical_plan_hash: str = "0" * 64) -> dict:
    """engine_provenance. `canonical_plan_hash` is excluded from the digest."""
    return {
        "solver": solver,
        "solver_status": solver_status,
        "random_seed": random_seed,
        "time_budget_seconds": time_budget_seconds,
        "deterministic_budget": deterministic_budget,
        "canonical_plan_hash": canonical_plan_hash,
        "objective_value": objective_value,
        "normalized_scores": {
            "delivery_score": 0.8,
            "overtime_score": 0.75,
            "changeover_score": 0.9,
            "stability_score": 0.9,
        },
    }


def make_content(
    *,
    plan_id: str = "PLAN-2026-09-14-001",
    plan_version: int = 1,
    profile: str = "Balanced",
    operations: list[dict] | None = None,
    unscheduled_operations: list[dict] | None = None,
    kpis: dict | None = None,
    engine: dict | None = None,
    stability_reference_plan_id: str | None = None,
    assumptions: list[str] | None = None,
    consequential_approval_required: bool = True,
    infeasible_reason: str | None = None,
    material_reservations: list[dict] | None = None,
    digest: str | None = None,
) -> dict:
    """A complete plan_content.

    `plan_digest` and `engine.canonical_plan_hash` are filled from the supplied
    `digest` (default "0"*64) so callers control them explicitly. Tests that
    verify real digesting use digest_module.canonical_plan_digest to compute the
    true value, then rebuild with it — see test_digest_identity.py.
    """
    d = digest if digest is not None else "0" * 64
    eng = make_engine() if engine is None else copy.deepcopy(engine)
    eng["canonical_plan_hash"] = d

    content = {
        "plan_id": plan_id,
        "plan_version": plan_version,
        "plan_digest": d,
        "profile": profile,
        "operations": (
            [make_operation(), make_operation(order_id="ORD-B", machine_id="MC-02",
                                              worker_id="WK-02", start_time=T1, end_time=T2,
                                              operation_no=1, lot_no=1)]
            if operations is None else copy.deepcopy(operations)
        ),
        "unscheduled_operations": (
            [make_unscheduled()] if unscheduled_operations is None
            else copy.deepcopy(unscheduled_operations)
        ),
        "kpis": make_kpis() if kpis is None else copy.deepcopy(kpis),
        "engine": eng,
        "stability_reference_plan_id": stability_reference_plan_id,
        "assumptions": (
            ["Horizon is 2026-09-14 to 2026-09-18 Asia/Singapore."]
            if assumptions is None else list(assumptions)
        ),
        "consequential_approval_required": consequential_approval_required,
        # const true in the schema: publishing always needs planner confirmation
        "publish_confirmation_required": True,
        "infeasible_reason": infeasible_reason,
        "material_reservations": (
            [make_reservation()] if material_reservations is None
            else copy.deepcopy(material_reservations)
        ),
    }
    return content


def make_lifecycle(*, plan_id: str = "PLAN-2026-09-14-001", plan_version: int = 1,
                   plan_digest: str = "0" * 64, status: str = "DRAFT",
                   approval_set_id: str | None = None, published_version: int | None = None,
                   updated_at: str = T0) -> dict:
    """A complete plan_lifecycle — mutable, never enters the digest."""
    return {
        "plan_id": plan_id,
        "plan_version": plan_version,
        "plan_digest": plan_digest,
        "status": status,
        "approval_set_id": approval_set_id,
        "published_version": published_version,
        "updated_at": updated_at,
    }


def make_plan(*, digest: str = "0" * 64, **kwargs) -> dict:
    """The full aggregate: content + lifecycle ($defs.plan)."""
    content = make_content(digest=digest, **kwargs)
    lifecycle = make_lifecycle(
        plan_id=content["plan_id"],
        plan_version=content["plan_version"],
        plan_digest=digest,
    )
    return {"content": content, "lifecycle": lifecycle}


def self_test() -> None:
    """Assert every fixture conforms. Called at import by the test suite."""
    validate(make_operation(), "schedule_operation")
    validate(make_reservation(), "lot_material_reservation")
    validate(make_unscheduled(), "unscheduled_operation")
    validate(make_kpis(), "kpis")
    validate(make_engine(), "engine_provenance")
    validate(make_content(), "plan_content")
    validate(make_lifecycle(), "plan_lifecycle")
    validate(make_plan(), "plan")


if __name__ == "__main__":  # pragma: no cover - manual smoke entry point
    self_test()
    print("all fixtures conform to the contract schemas")
    print("contract:", CONTRACT_PATH)
    print("schema_version:", _CONTRACT["schema_version"])
