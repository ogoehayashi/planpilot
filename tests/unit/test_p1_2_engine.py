"""P1-2 regression: exact pair setup, overtime limits and deterministic fallback."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import solve
from planpilot.authority import RuntimeAuthority
from planpilot.independent_validator import validate_plan_content
from planpilot.persistence import Database
from planpilot.runtime_planning import _restore_candidate, _solver_state, generate_authoritative_plans
from planpilot.store import canonical_plan_digest
from planpilot.v18_adapter import build_plan_content


ROOT = Path(__file__).resolve().parents[2]


def _factory(*, orders, shifts, changeovers=(), assumptions=None, horizon=120):
    return factory_from_dict({
        "planning_start": "2026-09-14T00:00:00+08:00",
        "horizon": horizon, "orders": orders,
        "machines": [{"machine_id": "M"}], "workers": [{"worker_id": "W"}],
        "shifts": list(shifts), "changeovers": list(changeovers),
        "assumptions": assumptions or {},
    })


def _order(order_id, product_id, due_at=100, duration=10):
    return {"order_id": order_id, "product_id": product_id, "quantity": 1,
            "due_at": due_at, "operations": [
                {"operation_no": 1, "machine_id": "M", "worker_id": "W", "duration": duration}
            ]}


def _shifts(end=120):
    return [
        {"machine_id": "M", "start": 0, "end": end},
        {"worker_id": "W", "start": 0, "end": end},
    ]


def test_pair_matrix_is_not_replaced_by_target_product_maximum():
    factory = _factory(
        orders=[_order("A", "PA", 10), _order("B", "PB", 100)],
        shifts=_shifts(),
        changeovers=[
            {"machine_id": "M", "from_product_id": source,
             "to_product_id": target, "minutes": minutes}
            for source, target, minutes in (
                ("PA", "PA", 0), ("PA", "PB", 3),
                ("PB", "PA", 17), ("PB", "PB", 0),
            )
        ],
    )
    result = solve(factory, "Balanced")
    assert not result.violations
    assert len(result.operations) == 2
    first, second = result.operations
    products = {"A": "PA", "B": "PB"}
    expected = 3 if (products[first["order_id"]], products[second["order_id"]]) == ("PA", "PB") else 17
    assert second["start"] - first["end"] >= expected
    assert result.kpis["total_changeover_min"] == expected


def test_legacy_changeover_bound_does_not_discard_feasible_order():
    factory = _factory(
        orders=[_order("A", "PA", 5, 5), _order("B", "PB", 40, 5)],
        shifts=_shifts(50), horizon=50,
    )
    second = factory.orders[1]
    operations = (replace(second.operations[0], release_at=15, changeover_minutes=5),)
    factory = replace(factory, orders=(factory.orders[0], replace(second, operations=operations)))
    result = solve(factory, "Balanced")
    assert len(result.operations) == 2
    assert result.kpis["late_orders"] == []
    assert result.kpis["total_changeover_min"] == 5
    assert result.violations == []


def test_overtime_cap_is_a_solver_constraint():
    shifts = [
        {"machine_id": "M", "start": 0, "end": 120},
        {"worker_id": "W", "start": 0, "end": 60, "window_type": "REGULAR"},
        {"worker_id": "W", "start": 60, "end": 120,
         "window_type": "OVERTIME", "overtime_allowed": True},
    ]
    blocked = _factory(
        orders=[_order("A", "PA", duration=80)], shifts=shifts,
        assumptions={"overtime_cap_hours": 0, "max_overtime_min_per_worker_per_day": 0},
    )
    result = solve(blocked, "Balanced")
    assert result.operations == []
    assert len(result.unscheduled_operations) == 1
    allowed = _factory(
        orders=[_order("A", "PA", duration=80)], shifts=shifts,
        assumptions={"overtime_cap_hours": 1, "max_overtime_min_per_worker_per_day": 20},
    )
    accepted = solve(allowed, "Balanced")
    assert len(accepted.operations) == 1
    assert accepted.kpis["overtime_hours"] == 20 / 60


def test_zero_primary_budget_uses_labelled_deterministic_dispatch():
    factory = _factory(orders=[_order("A", "PA")], shifts=_shifts())
    first = solve(factory, "Balanced", deterministic_budget=0)
    second = solve(factory, "Balanced", deterministic_budget=0)
    assert first.solver_status == "HEURISTIC_FALLBACK"
    assert first.operations == second.operations
    assert first.operations and first.violations == []


def test_reference_stability_breaks_equal_objective_tie():
    factory = _factory(
        orders=[_order("A", "PA"), _order("B", "PA")], shifts=_shifts(),
    )
    result = solve(
        factory, "Balanced", deterministic_budget=2,
        reference_starts={("A", 1): 10, ("B", 1): 0},
        stability_drift_min=0,
    )
    assert result.solver_status == "OPTIMAL"
    assert [(row["order_id"], row["start"]) for row in result.operations] == [
        ("B", 0), ("A", 10),
    ]


def test_fallback_provenance_survives_v18_adapter_and_independent_validation():
    state = json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))
    expanded, identities = _solver_state(state)
    candidate = _restore_candidate(
        asdict(solve(factory_from_dict(expanded), "Balanced", deterministic_budget=0)),
        identities,
    )
    content = build_plan_content(candidate, state, "PLAN-P1-2-FALLBACK", 1)
    assert content["engine"]["solver"] == "PRIORITY_DISPATCH_FALLBACK"
    assert content["engine"]["solver_status"] == "HEURISTIC_FALLBACK"
    assert validate_plan_content(content, state)["is_feasible"] is True


def test_independent_validator_catches_re_signed_pair_setup_mutation():
    state = json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))
    expanded, identities = _solver_state(state)
    candidate = _restore_candidate(
        asdict(solve(factory_from_dict(expanded), "Balanced")), identities,
    )
    content = build_plan_content(candidate, state, "PLAN-P1-2-MUTATION", 1)
    changed = next(row for row in content["operations"] if row["changeover_min"] > 0)
    minutes = changed["changeover_min"]
    changed["changeover_min"] = 0
    content["kpis"]["total_changeover_min"] -= minutes
    content["kpis"]["changeover_count"] -= 1
    digest = canonical_plan_digest(content)
    content["plan_digest"] = digest
    content["engine"]["canonical_plan_hash"] = digest
    result = validate_plan_content(content, state)
    assert result["is_feasible"] is False
    assert any(row["message"].startswith("HC-011:") for row in result["hard_violations"])


def test_partial_routing_is_not_mistaken_for_valid_unscheduled_lot():
    state = json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))
    for bucket in state["inventory"]["AL-6061"]["batches"]:
        bucket["quantity"] = 0
    expanded, identities = _solver_state(state)
    candidate = _restore_candidate(
        asdict(solve(factory_from_dict(expanded), "Balanced", deterministic_budget=0)),
        identities,
    )
    content = build_plan_content(candidate, state, "PLAN-P1-2-SHORTAGE", 1)
    result = validate_plan_content(content, state)
    assert content["unscheduled_operations"]
    assert result["is_feasible"] is False
    assert result["hard_violations"] == []
    assert content["engine"]["solver"] == "PRIORITY_DISPATCH_FALLBACK"


def test_explicit_stability_baseline_is_bound_to_authoritative_content(tmp_path):
    state = json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))
    db = Database(tmp_path / "authority.db")
    try:
        authority = RuntimeAuthority(db)
        first = generate_authoritative_plans(state, authority)
        second = generate_authoritative_plans(
            state, authority, baseline_plan_id=first["plan_id"]
        )
        assert second["version"] == first["version"] + 1
        baseline = authority.get_plan(first["plan_id"], 1)["content"]
        for option in second["plan_options"]:
            current = authority.get_plan(option["plan_id"], 2)["content"]
            assert current["stability_reference_plan_id"] == baseline["plan_id"]
            result = validate_plan_content(current, state, baseline)
            assert result["is_feasible"]
            assert result["kpis"]["schedule_stability"] == current["kpis"]["schedule_stability"]
        third = generate_authoritative_plans(state, authority)
        for option in third["plan_options"]:
            current = authority.get_plan(option["plan_id"], 3)["content"]
            assert current["stability_reference_plan_id"] is None
            assert current["kpis"]["schedule_stability"] == 1.0
    finally:
        db.close()
