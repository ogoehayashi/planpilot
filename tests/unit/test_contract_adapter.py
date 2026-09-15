from planpilot.contract_adapter import hard_constraint_report, minute_timestamp, recompute_kpis
from planpilot.domain.importer import load_factory


def test_formal_timestamp_and_thirteen_constraint_report():
    factory = load_factory("data/factory_demo_v18.json")
    assert minute_timestamp("2026-09-14T00:00:00+08:00", 60) == "2026-09-14T01:00:00+08:00"
    report = hard_constraint_report([], factory)
    assert report["is_feasible"] and report["checked_constraints"] == [f"HC-{i:03d}" for i in range(1, 14)]


def test_kpi_output_has_complete_contract_keys():
    factory = load_factory("data/factory_demo_v18.json")
    kpis = recompute_kpis([], factory)
    assert set(kpis) == {"on_time_rate", "eligible_orders", "on_time_orders", "eligible_order_coverage_rate", "late_orders", "total_tardiness_min", "overtime_hours", "changeover_count", "total_changeover_min", "schedule_stability", "unscheduled_operations", "secondary_skill_assignment_count"}
