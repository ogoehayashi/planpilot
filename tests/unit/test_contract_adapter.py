from dataclasses import asdict
import json
from pathlib import Path

from planpilot.contract_adapter import hard_constraint_report, minute_timestamp, to_plan_content
from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import solve
from planpilot.runtime_planning import _restore_candidate, _solver_state
from planpilot.validation.schema import is_valid


ROOT = Path(__file__).resolve().parents[2]


def test_facade_builds_nonempty_schema_valid_content_and_executes_all_constraints():
    state = json.loads((ROOT / "data/factory_demo_v18.json").read_text(encoding="utf-8"))
    solver_state, identities = _solver_state(state)
    candidate = _restore_candidate(asdict(solve(factory_from_dict(solver_state), "Balanced")), identities)
    content = to_plan_content(candidate, state, "PLAN-ADAPTER-FACADE", 1)
    report = hard_constraint_report(content, state)
    assert content["operations"]
    assert is_valid(content, "plan_content")
    assert report == {
        "is_feasible": True,
        "hard_violations": [],
        "checked_constraints": [f"HC-{number:03d}" for number in range(1, 14)],
    }


def test_formal_timestamp():
    assert minute_timestamp("2026-09-14T00:00:00+08:00", 60) == "2026-09-14T01:00:00+08:00"
