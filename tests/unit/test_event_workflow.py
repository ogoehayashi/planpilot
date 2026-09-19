import copy

from planpilot.domain.importer import load_factory
from planpilot.workflow.events import EventWorkflow, apply_event
from planpilot.audit import SecurityEventService
from planpilot.persistence import Database


def events():
    import json
    data = json.loads(open("data/factory_demo_v18.json", encoding="utf8").read())
    return data, {e["event_id"]: e for e in __import__("tools.generate_compliant_dataset", fromlist=["rows"]).rows(data)["Events"]}


def test_event_replans_from_received_and_preserves_order_identity():
    data, event_map = events()
    original = copy.deepcopy(data)
    result = EventWorkflow(data).replan(event_map["EVT-006"])
    assert result["state"] == "AWAITING_APPROVAL"
    assert result["event_id"] == "EVT-006"
    assert result["recommended_profile"] in {"Balanced", "Delivery First", "Cost First"}
    assert data == original


def test_breakdown_and_absence_become_resource_blocks():
    data, event_map = events()
    for event_id in ("EVT-002", "EVT-004"):
        result = EventWorkflow(data).replan(event_map[event_id])
        assert result["state"] in {"AWAITING_APPROVAL", "RECOMMENDED"}
        assert result["event_id"] == event_id


def test_prompt_injection_is_quarantined_logged_and_planning_continues():
    data, event_map = events()
    db = Database(":memory:")
    try:
        result = EventWorkflow(data, SecurityEventService(db)).replan(event_map["EVT-005"])
        assert result["state"] == "AWAITING_APPROVAL"
        assert result["candidates"]
        assert result["quarantine_impact"] == ["EVT-005"]
        assert result["security_events"][0]["security_event_id"].startswith("SEC-")
        assert db.verify_audit()
    finally:
        db.close()


def test_event_changes_are_atomic_and_unknown_references_rejected():
    data, event_map = events()
    before = copy.deepcopy(data)
    changed, _ = apply_event(data, event_map["EVT-007"])
    assert changed != before and data == before
    bad = dict(event_map["EVT-001"], order_id="UNKNOWN")
    try:
        apply_event(data, bad)
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("unknown event reference was accepted")
