"""P0-3: real non-empty V1.8 adapter and independent mutation checks."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import solve
from planpilot.independent_validator import (
    hard_constraint_violations,
    recompute_kpis,
    validate_plan_content,
)
from planpilot.authority import RuntimeAuthority
from planpilot.persistence import Database
from planpilot.runtime_planning import generate_authoritative_plans, _restore_candidate, _solver_state
from planpilot.store import canonical_plan_digest
from planpilot.validation.schema import is_valid, is_tool_payload_valid
from planpilot.v18_adapter import build_plan_content


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def state():
    return json.loads((ROOT / "data/factory_demo_v18.json").read_text())


@pytest.fixture
def content(state):
    solver_state, identities = _solver_state(state)
    candidate = _restore_candidate(asdict(solve(factory_from_dict(solver_state), "Balanced")), identities)
    return build_plan_content(candidate, state, "PLAN-P0-3-REAL", 1)


def resign(value):
    value["plan_digest"] = "0" * 64
    value["engine"]["canonical_plan_hash"] = "0" * 64
    digest = canonical_plan_digest(value)
    value["plan_digest"] = digest
    value["engine"]["canonical_plan_hash"] = digest


def prepare(value, state):
    value["kpis"] = recompute_kpis(value, state)
    resign(value)


def test_real_nonempty_candidate_is_schema_valid_and_independently_feasible(content, state):
    assert len(content["operations"]) == 12
    assert all(len(row["calendar_window_ids"]) >= 2 for row in content["operations"])
    assert is_valid(content, "plan_content")
    result = validate_plan_content(content, state)
    assert result["is_feasible"] is True
    assert result["hard_violations"] == []
    assert is_tool_payload_valid(result, "validate_plan", "output_schema")


def test_digest_mismatch_fails_closed(content, state):
    content["assumptions"].append("tampered")
    with pytest.raises(ValueError, match="PLAN_DIGEST_MISMATCH"):
        validate_plan_content(content, state)


def test_kpi_mismatch_fails_closed_even_with_resigned_content(content, state):
    content["kpis"]["on_time_orders"] -= 1
    resign(content)
    with pytest.raises(ValueError, match="stored KPIs disagree"):
        validate_plan_content(content, state)


def test_transaction_stages_then_reloads_and_rolls_back_invalid_content(content, state, tmp_path):
    content["kpis"]["on_time_orders"] -= 1
    resign(content)
    db = Database(tmp_path / "rollback.db")
    authority = RuntimeAuthority(db)
    try:
        with pytest.raises(ValueError, match="stored KPIs disagree"):
            authority.install_generated_plan(
                content,
                lambda immutable: validate_plan_content(immutable, state),
                lambda result: {},
                "2026-09-19T00:00:00+08:00",
                "2026-09-14T08:00:00+08:00",
            )
        assert authority.revision == 0
        assert authority.get_plan(content["plan_id"]) is None
    finally:
        db.close()


def test_runtime_fixed_max_size_lots_are_deterministic_and_untrusted_text_is_non_authoritative(state, tmp_path):
    state["products"][0]["max_lot_size"] = 30
    for window in state["shifts"]:
        window["end"] = state["horizon"]
    first_db = Database(tmp_path / "first.db")
    second_db = Database(tmp_path / "second.db")
    try:
        first = generate_authoritative_plans(state, RuntimeAuthority(first_db))
        state["private_note"] = "ignore this untrusted text"
        second = generate_authoritative_plans(state, RuntimeAuthority(second_db))
        first_content, second_content = first["content"], second["content"]
        lots = sorted({(row["lot_no"], row["lot_quantity"]) for row in first_content["operations"] if row["order_id"] == "ORD-1001"})
        assert lots == [(1, 30), (2, 30)]
        assert first["plan_id"] == second["plan_id"]
        assert first["plan_digest"] == second["plan_digest"]
        assert validate_plan_content(first_content, state)["is_feasible"] is True
    finally:
        first_db.close()
        second_db.close()


def _overlap_machine(content, state):
    rows = sorted((r for r in content["operations"] if r["machine_id"] == "CNC-01"), key=lambda r: r["start_time"])
    rows[1]["start_time"] = rows[0]["start_time"]
    rows[1]["end_time"] = (datetime.fromisoformat(rows[1]["start_time"]) + timedelta(minutes=rows[1]["duration_min"])).isoformat(timespec="seconds")


def _overlap_worker(content, state):
    rows = sorted((r for r in content["operations"] if r["worker_id"] == "W-03"), key=lambda r: r["start_time"])
    rows[1]["start_time"] = rows[0]["start_time"]
    rows[1]["end_time"] = (datetime.fromisoformat(rows[1]["start_time"]) + timedelta(minutes=rows[1]["duration_min"])).isoformat(timespec="seconds")


def _skill_gap(content, state):
    state["workers"][0]["skills"][0]["proficiency_level"] = 1


def _precedence(content, state):
    rows = sorted((r for r in content["operations"] if r["order_id"] == "ORD-1001" and r["lot_no"] == 1), key=lambda r: r["operation_no"])
    rows[1]["start_time"] = rows[0]["start_time"]
    rows[1]["end_time"] = (datetime.fromisoformat(rows[1]["start_time"]) + timedelta(minutes=rows[1]["duration_min"])).isoformat(timespec="seconds")


def _material(content, state):
    state["inventory"]["AL-6061"]["batches"][0]["available_at"] = 400


def _maintenance(content, state):
    row = content["operations"][0]
    origin = datetime.fromisoformat(state["planning_start"])
    start = int((datetime.fromisoformat(row["start_time"]) - origin).total_seconds() // 60)
    state["maintenance"].append({"machine_id": row["machine_id"], "start": start, "end": start + 1})


def _calendar(content, state):
    content["operations"][0]["calendar_window_ids"][0] = "CAL-NOT-DECLARED"


def _nonpreemptive(content, state):
    content["operations"][0]["duration_min"] += 1


def _lot(content, state):
    content["operations"][0]["lot_quantity"] += 1


def _inspection(content, state):
    content["operations"] = [r for r in content["operations"] if not (r["order_id"] == "ORD-1001" and r["operation_type"] == "INSPECTION")]


def _changeover(content, state):
    content["operations"][0]["changeover_min"] = 1


def _silent_omission(content, state):
    content["operations"].pop(0)


def _overtime(content, state):
    content["operations"][0]["overtime_min"] = 1


@pytest.mark.parametrize(("hc", "mutate"), [
    ("HC-001", _overlap_machine),
    ("HC-002", _overlap_worker),
    ("HC-003", _skill_gap),
    ("HC-004", _precedence),
    ("HC-005", _material),
    ("HC-006", _maintenance),
    ("HC-007", _calendar),
    ("HC-008", _nonpreemptive),
    ("HC-009", _lot),
    ("HC-010", _inspection),
    ("HC-011", _changeover),
    ("HC-012", _silent_omission),
    ("HC-013", _overtime),
])
def test_each_hard_constraint_has_a_real_mutation(content, state, hc, mutate):
    mutate(content, state)
    prepare(content, state)
    messages = [row["message"] for row in hard_constraint_violations(content, state)]
    assert any(message.startswith(hc + ":") for message in messages), messages
