"""Event-driven replanning boundary. Events are data and never instructions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

from planpilot.domain.importer import factory_from_dict
from planpilot.domain.planning import build_candidates
from planpilot.approval.policy import ordered_actions

EVENT_IDS = {f"EVT-{i:03d}" for i in range(1, 8)}
STATES = ("RECEIVED", "DATA_LOADED", "INPUT_VALIDATED", "PLANS_GENERATED", "PLANS_VALIDATED", "RECOMMENDED", "AWAITING_APPROVAL")
INJECTION_MARKERS = ("ignore", "system instruction", "publish immediately", "bypass", "override")


def _event_type(event):
    return str(event.get("event_type", "")).upper()


def apply_event(factory_data: dict, event: dict):
    """Apply exactly one supported event to a fresh copy and return audit metadata."""
    if not isinstance(event, dict) or event.get("event_id") not in EVENT_IDS:
        raise ValueError("unknown event_id")
    raw = deepcopy(factory_data)
    event_id = event["event_id"]
    kind = _event_type(event)
    security = None
    payload = event.get("Payload_JSON", "")
    if event_id == "EVT-005" or kind == "PROMPT_INJECTION" or any(marker in str(payload).lower() for marker in INJECTION_MARKERS):
        security = {"event_id": event_id, "payload": payload, "context": f"Events.Payload_JSON event_id={event_id}"}
        return raw, security
    if event_id == "EVT-001":
        order_id = event.get("order_id")
        if not any(o.get("order_id") == order_id for o in raw.get("orders", [])):
            raise ValueError("urgent order reference is unknown")
        for order in raw["orders"]:
            if order["order_id"] == order_id:
                order["priority"] = max(int(order.get("priority", 0)), 5)
    elif event_id == "EVT-002":
        raw.setdefault("maintenance", []).append({"machine_id": event["machine_id"], "start": int(event.get("start", 300)), "end": int(event.get("end", 360))})
    elif event_id == "EVT-003":
        for material in raw.get("inventory", {}).values():
            for batch in material.get("batches", []):
                if batch.get("batch_id") == event.get("source_id") or event.get("material_id") and material is raw["inventory"].get(event["material_id"]):
                    batch["available_at"] = max(batch.get("available_at", 0), int(event.get("available_at", 1440)))
    elif event_id == "EVT-004":
        raw.setdefault("maintenance", []).extend({"worker_id": event["worker_id"], "start": int(event.get("start", 120)), "end": int(event.get("end", 240))} for _ in [0])
    elif event_id == "EVT-006":
        order = next((o for o in raw.get("orders", []) if o.get("order_id") == event.get("order_id")), None)
        if order is None or type(event.get("quantity")) is not int or event["quantity"] <= 0:
            raise ValueError("quantity revision is invalid")
        order["quantity"] = event["quantity"]
    elif event_id == "EVT-007":
        order = next((o for o in raw.get("orders", []) if o.get("order_id") == event.get("order_id")), None)
        if order is None or not isinstance(event.get("due_date"), str):
            raise ValueError("due-date pull-in is invalid")
        datetime.fromisoformat(event["due_date"])
        order["due_date"] = event["due_date"]
    else:
        raise ValueError("unsupported event payload")
    return raw, security


class EventWorkflow:
    """Synchronous state machine with one event, one reload and one replan."""
    def __init__(self, factory_data, security_logger=None):
        self.factory_data = deepcopy(factory_data)
        self.security_logger = security_logger
        self.state = "RECEIVED"
        self.security_events = []
        self.quarantine_impact = []
        self.traces = []

    def _transition(self, expected, next_state):
        if self.state != expected:
            raise RuntimeError(f"invalid workflow transition {self.state} -> {next_state}")
        self.state = next_state

    def replan(self, event=None):
        self._transition("RECEIVED", "DATA_LOADED")
        data = self.factory_data
        if event is not None:
            data, security = apply_event(data, event)
            if security:
                if self.security_logger is None:
                    raise RuntimeError("prompt injection requires the authoritative security logger")
                logged = self.security_logger.log_untrusted_instruction(
                    security["payload"], context=security["context"], workflow_state=self.state,
                )
                self.security_events.append(logged)
                self.quarantine_impact.append(event["event_id"])
        self.factory_data = data
        self._transition("DATA_LOADED", "INPUT_VALIDATED")
        factory = factory_from_dict(data)
        self._transition("INPUT_VALIDATED", "PLANS_GENERATED")
        candidates = build_candidates(factory)
        self.traces.append({"tool": "generate_plan_options", "event_id": event.get("event_id") if event else None, "candidate_count": len(candidates)})
        self._transition("PLANS_GENERATED", "PLANS_VALIDATED")
        invalid = [p.profile for p in candidates if p.violations]
        if invalid:
            # Invalid alternatives remain visible; recommendation is based only on valid candidates.
            self.traces.append({"tool": "validate_plan", "invalid_profiles": invalid})
        self._transition("PLANS_VALIDATED", "RECOMMENDED")
        recommended = max((p for p in candidates if not p.violations and p.operations), key=lambda p: (p.kpis["eligible_order_coverage_rate"], p.kpis["on_time_rate"]), default=None)
        actions = ordered_actions({a for p in candidates if p is recommended for a in p.required_actions}) if recommended else []
        # Every publication requires planner confirmation even when no other action is consequential.
        if recommended and "publish_plan" not in actions:
            actions = ordered_actions([*actions, "publish_plan"])
        if recommended:
            self._transition("RECOMMENDED", "AWAITING_APPROVAL")
        return {"state": self.state, "event_id": event.get("event_id") if event else None, "candidates": [asdict(p) for p in candidates], "recommended_profile": recommended.profile if recommended else None, "required_approvals": actions, "quarantine_impact": list(self.quarantine_impact), "security_events": self.security_events, "traces": self.traces}
