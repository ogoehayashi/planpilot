"""G2 Phase 2 task 2.1 — ApprovalService.require_validated_binding.

Design §4 step 5 pins the publisher's validator-evidence re-check to
this read-only, fail-closed method: evidence lives in
``_validated[(plan_id, version, digest)]``, NOT in lifecycle, and every
violation raises ``ApprovalInvariantError`` (→ INTERNAL_ERROR/503 via
adapt_exception fall-through) with zero state change. §8 case 17 names
the headline negative (approval set survives, validation record deleted
→ publish cannot proceed); the mutation variants below pin each of the
five evidence conditions individually.
"""

from __future__ import annotations

import copy

import pytest

from planpilot.approval import (
    ApprovalInvariantError,
    ApprovalService,
)
from planpilot.store import PlanStore, canonical_plan_digest

from .test_approval_service import (  # reuse the pinned dataset helpers
    HORIZON_END,
    NOW,
    _impact,
    _signed_content,
    _validation_result,
)

ACTIONS = ("add_overtime", "publish_plan")


@pytest.fixture
def validated(fixtures, tmp_path):
    """PlanStore + ApprovalService holding one fully valid record and one
    APPROVED-complete set derived from it (design case-E precondition)."""
    store = PlanStore()
    content = _signed_content(fixtures)
    store.put_content(content)
    store.create_lifecycle(
        content["plan_id"], content["plan_version"], content["plan_digest"], NOW
    )
    service = ApprovalService(store, tmp_path / "approvals.json")
    result = _validation_result(content)
    impacts = {action: _impact(action) for action in ACTIONS}
    service.record_validated_plan(result, impacts, HORIZON_END, NOW)
    binding = (content["plan_id"], content["plan_version"], content["plan_digest"])
    return store, service, content, binding


def _tamper(service, binding, mutate):
    """Fault-inject into the stored evidence, bypassing write guards —
    models the persisted-state defect the guard must catch."""
    record = service._validated[binding]
    mutate(record["validation_result"])


def test_happy_path_returns_copy_of_record(validated):
    store, service, content, binding = validated
    record = service.require_validated_binding(*binding)
    assert record["validation_result"]["plan_digest"] == content["plan_digest"]
    assert record["impact_by_action"].keys() == set(ACTIONS)
    # A copy: mutating the returned record must not poison stored evidence.
    record["validation_result"]["digest_verified"] = False
    again = service.require_validated_binding(*binding)
    assert again["validation_result"]["digest_verified"] is True


def test_method_is_read_only(validated):
    store, service, content, binding = validated
    before_sets = copy.deepcopy(service._sets)
    before_validated = copy.deepcopy(service._validated)
    service.require_validated_binding(*binding)
    assert service._sets == before_sets
    assert service._validated == before_validated


def test_missing_validation_record_raises_invariant(validated):
    """§8 case 17 headline: the APPROVED set survives, the evidence is
    gone — the binding guard is what stops the publish."""
    store, service, content, binding = validated
    approval_set = service.request_approval(
        content["plan_id"], content["plan_version"], content["plan_digest"],
        "publish_plan", NOW, None,
    )
    assert approval_set["approval_set_id"]
    del service._validated[binding]
    with pytest.raises(ApprovalInvariantError):
        service.require_validated_binding(*binding)


@pytest.mark.parametrize(
    "field,value",
    [
        ("digest_verified", False),
        ("is_feasible", False),
        ("hard_violations", [{"rule": "cool_chain_breach", "detail": "tampered"}]),
        ("recomputed_plan_digest", "0" * 64),
    ],
)
def test_evidence_condition_violations_fail_closed(validated, field, value):
    store, service, content, binding = validated
    _tamper(service, binding, lambda result: result.__setitem__(field, value))
    with pytest.raises(ApprovalInvariantError):
        service.require_validated_binding(*binding)


def test_derived_set_binding_violation_fails_closed(validated):
    """Set exists for the binding but its required_actions no longer match
    the evidence's complete action set (6th condition)."""
    store, service, content, binding = validated
    service.request_approval(
        content["plan_id"], content["plan_version"], content["plan_digest"],
        "add_overtime", NOW, None,
    )
    set_id = service._set_by_binding[binding]
    # Widen the evidence AFTER the set was derived: set and evidence now
    # disagree on membership — the load_state-time invariant is broken.
    _tamper(
        service,
        binding,
        lambda result: result["approval_requirements"].append(
            {
                "action": "change_promised_due_date",
                "approver_role": "Production Manager",
                "reason": "injected drift",
            }
        ),
    )
    with pytest.raises(ApprovalInvariantError):
        service.require_validated_binding(*binding)
    # Read-only even while refusing: the set record itself survived.
    assert service._sets[set_id]["plan_digest"] == content["plan_digest"]
    assert [a["action"] for a in service._sets[set_id]["approvals"]] == \
        service._sets[set_id]["required_actions"]


def test_wrong_binding_never_resolves(validated):
    store, service, content, binding = validated
    with pytest.raises(ApprovalInvariantError):
        service.require_validated_binding(binding[0], binding[1], "f" * 64)
    with pytest.raises(ApprovalInvariantError):
        service.require_validated_binding("plan_other", binding[1], binding[2])
