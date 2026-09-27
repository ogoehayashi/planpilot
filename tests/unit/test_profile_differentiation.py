"""Regression guard: the three plan profiles must not collapse into one plan.

Observed defect (found 2026-09-27)
----------------------------------
On the shipped demo dataset the three profiles returned *identical* KPIs, so
"compare three candidate plans" compared a plan with itself.  Two compounding
causes, both reproduced here:

1. **The dataset had no tier-3 levers left.**  ``data/factory_demo_case.json``
   declared no OVERTIME shift windows with ``overtime_cap_hours = 0``, so the
   overtime term in the profile-weighted objective was identically zero; and
   ``changeover_reference_min`` was 60 while real plans total 190-665 changeover
   minutes, so ``capped_setup = min(total, 60)`` saturated and the changeover term
   became a constant.  ``priority`` was also perfectly anti-correlated with
   ``due_at``, so "priority first" and "due date first" produced one sequence.
   The contract settles unscheduled work and lateness (Tier 0-2) *before* the
   profile weights are consulted (Tier 3), so with every tier-3 term constant all
   three profiles minimised the same expression: identical plans **at any solver
   budget**.  Measured: identical at 0.2 s, 1 s, 5 s, 20 s and 40 s.

2. **The production path never reached Tier 3.**  ``build_candidates`` used the
   conservative 0.2 s default, i.e. 0.04 s per lexicographic stage, so the solver
   bailed to the deterministic dispatcher before the profile weights applied.

A third defect made the fix impossible until it was found: the overtime encoding
built a ``horizon + 1`` (7,201 entry) lookup table per operation, costing ~13 s of
Python model construction for 26 operations.  Overtime is now expressed as the
overlap of each operation with each overtime window, which is exact, so the
primary solver actually runs.

These tests fail if any of that class of defect returns.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import (
    DEFAULT_DETERMINISTIC_BUDGET, PROFILES, build_candidates, solve,
)
from planpilot.runtime_planning import _restore_candidate, _solver_state
from planpilot.v18_adapter import build_plan_content

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "data" / "factory_demo_case.json"
KPI_SIGNATURE_FIELDS = (
    "on_time_rate", "eligible_orders", "late_orders", "total_tardiness_min",
    "overtime_hours", "changeover_count", "total_changeover_min",
    "unscheduled_operations", "secondary_skill_assignment_count",
)


def signature(kpis: dict) -> str:
    return json.dumps(
        {field: (round(kpis[field], 4) if isinstance(kpis[field], float) else kpis[field])
         for field in KPI_SIGNATURE_FIELDS},
        sort_keys=True,
    )


@pytest.fixture(scope="module")
def demo_state():
    return json.loads(DEMO.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def demo_factory(demo_state):
    expanded, _ = _solver_state(demo_state)
    return factory_from_dict(expanded)


def test_demo_dataset_declares_the_trade_off_levers(demo_state):
    """Every lever the profile-weighted tier depends on must be live in the data."""
    shifts = Counter(row.get("window_type", "REGULAR") for row in demo_state["shifts"])
    assert shifts["OVERTIME"] > 0, (
        "no OVERTIME shift windows: the overtime term of the profile objective is "
        "identically zero, so the profiles cannot differ on it"
    )
    assumptions = demo_state["assumptions"]
    assert assumptions["overtime_cap_hours"] > 0, "overtime is capped at zero hours"
    assert assumptions["max_overtime_min_per_worker_per_day"] > 0, (
        "a zero daily overtime maximum makes overtime unusable"
    )

    # The reference must not saturate on a realistic plan, or capped_setup becomes
    # a constant and the changeover term stops discriminating.  The contract defines
    # it as "the sum of the maximum applicable changeover penalty for each eligible
    # PRODUCTION operation in the planning horizon" (inspection operations excluded).
    worst = {}
    for row in demo_state["changeovers"]:
        key = (row["machine_id"], row["to_product_id"])
        worst[key] = max(worst.get(key, 0), row["minutes"])
    achievable = sum(worst.get((op["machine_id"], order["product_id"]), 0)
                     for order in demo_state["orders"] for op in order["operations"]
                     if op["operation_type"] == "PRODUCTION")
    reference = assumptions["changeover_reference_min"]
    assert reference == achievable, (
        "changeover_reference_min=%s is not the contract's definition (%s): the sum "
        "of the maximum applicable changeover penalty per eligible PRODUCTION "
        "operation" % (reference, achievable)
    )
    assert reference >= 500, (
        "changeover_reference_min=%s is so small that capped_setup saturates on an "
        "ordinary plan, which makes the changeover term a constant" % reference
    )

    # Priority must not be a proxy for the due date, or "priority first" and
    # "due date first" are the same sequence and sequencing cannot differentiate.
    by_priority = [o["order_id"] for o in sorted(demo_state["orders"], key=lambda o: (-o["priority"], o["due_at"]))]
    by_due = [o["order_id"] for o in sorted(demo_state["orders"], key=lambda o: (o["due_at"], -o["priority"]))]
    assert by_priority != by_due, "priority ordering equals due-date ordering"


def test_production_path_returns_three_distinct_plans(demo_state, demo_factory):
    """The whole point of offering three profiles: they must differ."""
    plans = build_candidates(demo_factory)
    assert [plan.profile for plan in plans] == list(PROFILES)
    signatures = [signature(plan.kpis) for plan in plans]
    assert len(set(signatures)) == len(PROFILES), (
        "profiles %s returned %d distinct KPI signatures, not %d:\n%s"
        % (list(PROFILES), len(set(signatures)), len(PROFILES), "\n".join(signatures))
    )
    reference = demo_state["assumptions"]["changeover_reference_min"]
    for plan in plans:
        assert plan.violations == [], (plan.profile, plan.violations)
        assert plan.kpis["total_changeover_min"] <= reference, (
            "%s totals %s changeover minutes, above the reference %s: capped_setup "
            "saturated, so the changeover term stopped discriminating"
            % (plan.profile, plan.kpis["total_changeover_min"], reference)
        )


def test_every_profile_schedules_every_eligible_operation(demo_state, demo_factory):
    """The contract rejects a plan that leaves an eligible operation unscheduled."""
    for plan in build_candidates(demo_factory):
        assert plan.unscheduled_operations == [], plan.profile
    expected = sum(len(order["operations"]) for order in demo_state["orders"])
    for plan in build_candidates(demo_factory):
        assert len(plan.operations) == expected, plan.profile


def test_all_profiles_deliver_the_same_service_level(demo_factory):
    """Tier 1 settles lateness before the profile weights apply, for every profile."""
    plans = build_candidates(demo_factory)
    assert {plan.kpis["unscheduled_operations"] for plan in plans} == {0}
    assert {plan.kpis["on_time_orders"] for plan in plans} == {len(demo_factory.orders)}
    assert {plan.kpis["total_tardiness_min"] for plan in plans} == {0}


def test_production_budget_is_large_enough_to_reach_the_profile_tier():
    assert DEFAULT_DETERMINISTIC_BUDGET >= 2.0, (
        "a budget below ~2 s leaves 0.4 s per lexicographic stage, which is not "
        "enough for tiers 0-2 to settle and tier 3 to run at all"
    )


def test_fallback_is_not_profile_blind(demo_factory):
    """The labelled rung-2 fallback must still honour the profile weights."""
    first = solve(demo_factory, "Delivery First", deterministic_budget=0)
    cost = solve(demo_factory, "Cost First", deterministic_budget=0)
    assert first.solver_status == cost.solver_status == "HEURISTIC_FALLBACK"
    assert signature(first.kpis) != signature(cost.kpis), (
        "the deterministic fallback returned the same plan for two profiles"
    )


def test_plans_are_reproducible_and_carry_their_real_budget(demo_state, demo_factory):
    """Determinism, and provenance that matches the budget actually used."""
    first, second = build_candidates(demo_factory), build_candidates(demo_factory)
    assert [asdict(plan) for plan in first] == [asdict(plan) for plan in second]
    expanded, identities = _solver_state(demo_state)
    for plan, profile in zip(first, PROFILES):
        assert plan.deterministic_budget == DEFAULT_DETERMINISTIC_BUDGET
        candidate = _restore_candidate(asdict(plan), identities)
        content = build_plan_content(candidate, demo_state, f"PLAN-DIFF-{profile}", 1)
        assert content["engine"]["deterministic_budget"] == DEFAULT_DETERMINISTIC_BUDGET, (
            "plan content reports a solver budget that was not the one used"
        )
