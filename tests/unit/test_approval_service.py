"""Contract and crash-order tests for the approval-service spec."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from planpilot.approval import (
    APPROVAL_ACTIONS,
    DECISION_REASON_CODES,
    DEFAULT_TTL_SECONDS,
    INVALIDATION_CAUSES,
    MAXIMUM_TTL_SECONDS,
    MINIMUM_TTL_SECONDS,
    REASON_BY_ACTION,
    ROLE_BY_ACTION,
    ApprovalDecisionConflictError,
    ApprovalExpiredError,
    ApprovalInvariantError,
    ApprovalRejectedError,
    ApprovalRequiredError,
    ApprovalService,
    ApprovalSetInvalidatedError,
    ApprovalStateNotFoundError,
    ApprovalWindowClosedError,
)
from planpilot.store import PlanStore, canonical_plan_digest
from planpilot.validation import (
    SchemaValidationError,
    is_tool_payload_valid,
    tool_names,
    validate_tool_payload,
)

NOW = "2026-09-14T08:00:00+08:00"
HORIZON_END = "2026-09-18T17:00:00+08:00"


def _signed_content(fixtures, *, plan_version=1, overtime=2.5, secondary=0):
    kpis = fixtures.make_kpis(
        overtime_hours=overtime,
        secondary_skill_assignment_count=secondary,
    )
    draft = fixtures.make_content(plan_version=plan_version, kpis=kpis)
    digest = canonical_plan_digest(draft)
    return fixtures.make_content(plan_version=plan_version, kpis=kpis, digest=digest)


def _requirement(action):
    return {
        "action": action,
        "approver_role": ROLE_BY_ACTION[action],
        "reason": f"Server-derived requirement for {action}.",
    }


def _impact(action):
    return {
        "affected_order_ids": ["ORD-A"],
        "changed_operation_count": 1,
        "kpi_deltas": {"overtime_hours": 2.5} if action == "add_overtime" else {},
        "reason_codes": [REASON_BY_ACTION[action]],
    }


def _validation_result(content, actions=("add_overtime", "publish_plan")):
    return {
        "plan_id": content["plan_id"],
        "plan_version": content["plan_version"],
        "plan_digest": content["plan_digest"],
        "recomputed_plan_digest": content["plan_digest"],
        "digest_verified": True,
        "is_feasible": True,
        "infeasible_reason": None,
        "hard_violations": [],
        "kpis": copy.deepcopy(content["kpis"]),
        "approval_requirements": [_requirement(action) for action in actions],
        "quarantine_impact": [],
    }


@pytest.fixture
def prepared(fixtures, tmp_path):
    store = PlanStore()
    content = _signed_content(fixtures)
    store.put_content(content)
    store.create_lifecycle(
        content["plan_id"], content["plan_version"], content["plan_digest"], NOW
    )
    service = ApprovalService(store, tmp_path / "approvals.json")
    result = _validation_result(content)
    impacts = {action: _impact(action) for action in ("add_overtime", "publish_plan")}
    service.record_validated_plan(result, impacts, HORIZON_END, NOW)
    return store, service, content, result, impacts


def _request(service, content, action="publish_plan", expires_at=None):
    return service.request_approval(
        content["plan_id"],
        content["plan_version"],
        content["plan_digest"],
        action,
        NOW,
        expires_at,
    )


class TestToolSchemaBoundary:
    def test_all_eight_tool_names_are_exposed(self):
        assert tool_names() == [
            "load_factory_state",
            "validate_factory_state",
            "generate_plan_options",
            "validate_plan",
            "request_approval",
            "check_approval_status",
            "publish_plan",
            "log_security_event",
        ]

    def test_validate_plan_output_is_checked_with_bundled_refs(self, prepared):
        _, _, _, result, _ = prepared
        assert is_tool_payload_valid(result, "validate_plan", "output_schema")

    def test_unknown_validate_plan_field_is_rejected(self, prepared):
        _, _, _, result, _ = prepared
        result = {**result, "caller_authored_approval": True}
        with pytest.raises(SchemaValidationError):
            validate_tool_payload(
                result, "validate_plan", "output_schema", "validate_plan_output"
            )

    def test_service_cannot_bypass_validate_plan_output_schema(self, prepared):
        store, _, _, result, impacts = prepared
        result = {**result, "caller_authored_approval": True}
        with pytest.raises(SchemaValidationError):
            ApprovalService(store).record_validated_plan(
                result, impacts, HORIZON_END, NOW
            )


class TestPolicyMatchesContract:
    def test_action_and_role_mapping(self, fixtures):
        contract = fixtures.contract()
        status = contract["$defs"]["approval_status"]
        assert tuple(status["properties"]["action"]["enum"]) == APPROVAL_ACTIONS
        assert ROLE_BY_ACTION == {
            "assign_qualified_secondary_skill": "Production Planner",
            "add_overtime": "Production Manager",
            "change_promised_due_date": "Production Manager",
            "publish_plan": "Production Planner",
        }

    def test_expiry_constants(self, fixtures):
        policy = fixtures.contract()["approval_expiry_policy"]
        assert DEFAULT_TTL_SECONDS == policy["default_ttl_seconds"]
        assert MINIMUM_TTL_SECONDS == policy["minimum_ttl_seconds"]
        assert MAXIMUM_TTL_SECONDS == policy["maximum_ttl_seconds"]

    def test_decision_reasons_and_invalidation_causes(self, fixtures):
        contract = fixtures.contract()
        assert DECISION_REASON_CODES == frozenset(
            contract["$defs"]["approval_decision_reason_code"]["enum"]
        )
        causes = contract["$defs"]["error_details_approval_set_invalidated"][
            "properties"
        ]["invalidation_cause"]["enum"]
        assert INVALIDATION_CAUSES == frozenset(causes)


class TestCreationAndBinding:
    def test_trigger_cannot_narrow_complete_set(self, prepared, fixtures):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content, "publish_plan")
        assert snapshot["required_actions"] == ["add_overtime", "publish_plan"]
        assert [item["action"] for item in snapshot["approvals"]] == snapshot[
            "required_actions"
        ]
        fixtures.validate(snapshot, "approval_set_snapshot")

    def test_different_trigger_returns_identical_existing_set(self, prepared):
        _, service, content, _, _ = prepared
        first = _request(service, content, "publish_plan")
        second = _request(service, content, "add_overtime")
        assert second == first
        assert len(service) == 1

    def test_returned_snapshot_is_a_defensive_copy(self, prepared):
        _, service, content, _, _ = prepared
        first = _request(service, content)
        first["approvals"][0]["status"] = "APPROVED"
        second = _request(service, content)
        assert all(item["status"] == "PENDING" for item in second["approvals"])

    def test_required_role_mismatch_is_rejected(self, prepared):
        store, _, content, result, impacts = prepared
        result = copy.deepcopy(result)
        result["approval_requirements"][0]["approver_role"] = "Production Planner"
        service = ApprovalService(store)
        with pytest.raises(ApprovalInvariantError, match="wrong approver role"):
            service.record_validated_plan(result, impacts, HORIZON_END, NOW)

    def test_overtime_action_must_match_recomputed_kpi(self, prepared):
        store, _, content, result, _ = prepared
        result = copy.deepcopy(result)
        result["approval_requirements"] = [_requirement("publish_plan")]
        with pytest.raises(ApprovalInvariantError, match="add_overtime"):
            ApprovalService(store).record_validated_plan(
                result, {"publish_plan": _impact("publish_plan")}, HORIZON_END, NOW
            )

    def test_impact_set_must_be_exact(self, prepared):
        store, _, _, result, impacts = prepared
        bad = copy.deepcopy(impacts)
        bad.pop("add_overtime")
        with pytest.raises(ApprovalInvariantError, match="exactly"):
            ApprovalService(store).record_validated_plan(result, bad, HORIZON_END, NOW)

    def test_validation_kpis_must_equal_immutable_plan_content(self, prepared):
        store, _, _, result, impacts = prepared
        result = copy.deepcopy(result)
        result["kpis"]["overtime_hours"] = 999.0
        with pytest.raises(ApprovalInvariantError, match="KPIs disagree"):
            ApprovalService(store).record_validated_plan(
                result, impacts, HORIZON_END, NOW
            )


class TestExpiry:
    def test_default_ttls_are_per_action(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content)
        by_action = {item["action"]: item for item in snapshot["approvals"]}
        assert by_action["add_overtime"]["expires_at"] == "2026-09-14T12:00:00+08:00"
        assert by_action["publish_plan"]["expires_at"] == "2026-09-15T08:00:00+08:00"

    def test_proposed_expiry_is_clamped_to_minimum(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content, expires_at="2026-09-14T08:01:00+08:00")
        assert {item["expires_at"] for item in snapshot["approvals"]} == {
            "2026-09-14T08:05:00+08:00"
        }

    def test_proposed_expiry_is_clamped_to_maximum(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content, expires_at="2026-09-30T08:00:00+08:00")
        assert {item["expires_at"] for item in snapshot["approvals"]} == {
            "2026-09-16T08:00:00+08:00"
        }

    def test_closed_horizon_fails_without_creating_a_set(self, prepared):
        store, _, content, result, impacts = prepared
        service = ApprovalService(store)
        service.record_validated_plan(
            result, impacts, "2026-09-13T08:03:00+08:00", NOW
        )
        with pytest.raises(ApprovalWindowClosedError) as exc:
            _request(service, content)
        assert len(service) == 0
        assert exc.value.code == "APPROVAL_WINDOW_CLOSED"

    def test_authoritative_read_marks_pending_request_expired(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content, expires_at="2026-09-14T08:05:00+08:00")
        expired = service.check_approval_status(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            "2026-09-14T08:05:00+08:00",
        )
        assert expired["aggregate_status"] == "EXPIRED"
        assert all(item["status"] == "EXPIRED" for item in expired["approvals"])


class TestDecisionsAndPreconditions:
    def test_role_is_enforced(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content)
        overtime = snapshot["approvals"][0]
        with pytest.raises(ApprovalInvariantError, match="cannot decide"):
            service.record_decision(
                overtime["approval_request_id"],
                "APPROVED",
                "Production Planner",
                "planner@example.test",
                "2026-09-14T08:01:00+08:00",
            )

    def test_failed_wrong_role_call_cannot_expire_or_mutate_state(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(
            service, content, expires_at="2026-09-14T08:05:00+08:00"
        )
        overtime = snapshot["approvals"][0]
        with pytest.raises(ApprovalInvariantError, match="cannot decide"):
            service.record_decision(
                overtime["approval_request_id"],
                "APPROVED",
                "Production Planner",
                "planner@example.test",
                "2026-09-14T08:06:00+08:00",
            )
        unchanged = service.check_approval_status(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            "2026-09-14T08:04:00+08:00",
        )
        assert all(item["status"] == "PENDING" for item in unchanged["approvals"])

    def test_rejection_requires_controlled_reason(self, prepared):
        _, service, content, _, _ = prepared
        request = _request(service, content)["approvals"][0]
        with pytest.raises(ValueError, match="registered"):
            service.record_decision(
                request["approval_request_id"],
                "REJECTED",
                request["approver_role"],
                "manager@example.test",
                "2026-09-14T08:01:00+08:00",
                "NOT_A_REASON",
            )

    def test_same_decision_retry_is_idempotent_but_different_retry_conflicts(self, prepared):
        _, service, content, _, _ = prepared
        request = _request(service, content)["approvals"][0]
        args = (
            request["approval_request_id"],
            "APPROVED",
            request["approver_role"],
            "manager@example.test",
            "2026-09-14T08:01:00+08:00",
        )
        first = service.record_decision(*args)
        assert service.record_decision(*args) == first
        with pytest.raises(ApprovalDecisionConflictError):
            service.record_decision(
                request["approval_request_id"],
                "REJECTED",
                request["approver_role"],
                "manager@example.test",
                "2026-09-14T08:02:00+08:00",
                "OVERTIME_NOT_JUSTIFIED",
            )

    def test_pending_set_raises_approval_required_with_schema_details(self, prepared, fixtures):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content)
        with pytest.raises(ApprovalRequiredError) as exc:
            service.require_approved(
                snapshot["approval_set_id"], *(
                    content[k] for k in ("plan_id", "plan_version", "plan_digest")
                ), NOW
            )
        fixtures.validate(exc.value.details, "error_details_approval_required")

    def test_all_requests_approved_satisfies_precondition(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content)
        for request in snapshot["approvals"]:
            snapshot = service.record_decision(
                request["approval_request_id"],
                "APPROVED",
                request["approver_role"],
                f"{request['approver_role']} authenticated",
                "2026-09-14T08:01:00+08:00",
            )
        approved = service.require_approved(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            NOW,
        )
        assert approved["aggregate_status"] == "APPROVED"

    def test_rejected_precedes_expired_and_invalidated(self, prepared, fixtures):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content, expires_at="2026-09-14T08:05:00+08:00")
        first = snapshot["approvals"][0]
        service.record_decision(
            first["approval_request_id"],
            "REJECTED",
            first["approver_role"],
            "manager@example.test",
            "2026-09-14T08:01:00+08:00",
            "OVERTIME_NOT_JUSTIFIED",
        )
        service.invalidate_set(snapshot["approval_set_id"], "plan_regenerated", content["plan_digest"])
        result = service.check_approval_status(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            "2026-09-14T08:06:00+08:00",
        )
        assert result["aggregate_status"] == "REJECTED"
        fixtures.validate(result, "approval_set_snapshot")
        with pytest.raises(ApprovalRejectedError):
            service.require_approved(
                snapshot["approval_set_id"],
                content["plan_id"],
                content["plan_version"],
                content["plan_digest"],
                "2026-09-14T08:06:00+08:00",
            )

    def test_invalidated_live_set_raises_registered_error(self, prepared, fixtures):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content)
        service.invalidate_set(snapshot["approval_set_id"], "digest_changed", content["plan_digest"])
        with pytest.raises(ApprovalSetInvalidatedError) as exc:
            service.require_approved(
                snapshot["approval_set_id"],
                content["plan_id"],
                content["plan_version"],
                content["plan_digest"],
                NOW,
            )
        fixtures.validate(exc.value.details, "error_details_approval_set_invalidated")


class TestPersistenceAndOutbox:
    def test_failed_persist_rolls_back_new_set(self, prepared, monkeypatch):
        _, service, content, _, _ = prepared

        def fail():
            raise OSError("disk full")

        monkeypatch.setattr(service, "_persist", fail)
        with pytest.raises(OSError, match="disk full"):
            _request(service, content)
        assert len(service) == 0

    def test_failed_persist_rolls_back_decision(self, prepared, monkeypatch):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content)
        request = snapshot["approvals"][0]

        def fail():
            raise OSError("disk full")

        monkeypatch.setattr(service, "_persist", fail)
        with pytest.raises(OSError, match="disk full"):
            service.record_decision(
                request["approval_request_id"],
                "APPROVED",
                request["approver_role"],
                "manager@example.test",
                "2026-09-14T08:01:00+08:00",
            )
        assert service._sets[snapshot["approval_set_id"]]["approvals"][0]["status"] == "PENDING"

    def test_dump_load_redump_is_byte_identical(self, prepared, tmp_path):
        store, service, content, _, _ = prepared
        _request(service, content)
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        service.dump_state(first)
        reloaded = ApprovalService(store)
        reloaded.load_state(first)
        reloaded.dump_state(second)
        assert first.read_bytes() == second.read_bytes()

    def test_load_rejects_semantically_tampered_validation_record(self, prepared, tmp_path):
        store, service, content, _, _ = prepared
        _request(service, content)
        path = tmp_path / "tampered.json"
        service.dump_state(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["validated_plans"][0]["validation_result"]["kpis"]["overtime_hours"] = 999.0
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ApprovalInvariantError, match="KPIs disagree"):
            ApprovalService(store).load_state(path)

    @pytest.mark.parametrize("tamper", ["membership", "impact", "request_id"])
    def test_load_rejects_set_that_disagrees_with_validated_evidence(
        self, prepared, tmp_path, tamper
    ):
        store, service, content, _, _ = prepared
        _request(service, content)
        path = tmp_path / f"tampered-{tamper}.json"
        service.dump_state(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        record = raw["approval_sets"][0]
        if tamper == "membership":
            record["required_actions"] = ["publish_plan"]
            record["approvals"] = [record["approvals"][1]]
        elif tamper == "impact":
            record["approvals"][0]["impact_summary"]["changed_operation_count"] = 0
        else:
            record["approvals"][0]["approval_request_id"] = "APR-tampered"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ApprovalInvariantError):
            ApprovalService(store).load_state(path)

    def test_malformed_invalidation_digest_is_rejected(self, prepared):
        _, service, content, _, _ = prepared
        snapshot = _request(service, content)
        with pytest.raises(ApprovalInvariantError, match="malformed"):
            service.invalidate_set(
                snapshot["approval_set_id"], "digest_changed", "not-a-digest"
            )

    def test_crash_after_invalidation_before_ack_replays_safely(self, prepared, fixtures, tmp_path):
        store, service, content, _, _ = prepared
        snapshot = _request(service, content)
        store.transition(
            content["plan_id"],
            content["plan_version"],
            "AWAITING_APPROVAL",
            "2026-09-14T08:01:00+08:00",
            approval_set_id=snapshot["approval_set_id"],
        )
        next_content = _signed_content(fixtures, plan_version=2)
        store.commit_new_version(next_content, "2026-09-14T08:02:00+08:00")

        def crash(_event):
            raise RuntimeError("crash after durable invalidation")

        with pytest.raises(RuntimeError, match="crash after durable"):
            service.consume_superseded(
                "2026-09-14T08:03:00+08:00", after_invalidation_commit=crash
            )
        assert len(store.pending_superseded()) == 1

        reloaded = ApprovalService(store, tmp_path / "approvals.json")
        status = reloaded.check_approval_status(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            "2026-09-14T08:03:00+08:00",
        )
        assert status["aggregate_status"] == "INVALIDATED"
        assert reloaded.consume_superseded("2026-09-14T08:04:00+08:00") == 1
        assert store.pending_superseded() == []
        assert reloaded.consume_superseded("2026-09-14T08:05:00+08:00") == 0

    def test_crash_before_invalidation_leaves_event_and_live_set(
        self, prepared, fixtures, tmp_path, monkeypatch
    ):
        store, service, content, _, _ = prepared
        snapshot = _request(service, content)
        store.transition(
            content["plan_id"],
            content["plan_version"],
            "AWAITING_APPROVAL",
            "2026-09-14T08:01:00+08:00",
            approval_set_id=snapshot["approval_set_id"],
        )
        store.commit_new_version(
            _signed_content(fixtures, plan_version=2),
            "2026-09-14T08:02:00+08:00",
        )
        original = service.invalidate_set

        def crash_before(*_args, **_kwargs):
            raise RuntimeError("crash before invalidation")

        monkeypatch.setattr(service, "invalidate_set", crash_before)
        with pytest.raises(RuntimeError, match="before invalidation"):
            service.consume_superseded("2026-09-14T08:03:00+08:00")
        assert len(store.pending_superseded()) == 1
        reloaded = ApprovalService(store, tmp_path / "approvals.json")
        assert reloaded.check_approval_status(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            NOW,
        )["aggregate_status"] == "PENDING"
        monkeypatch.setattr(service, "invalidate_set", original)
        assert service.consume_superseded("2026-09-14T08:04:00+08:00") == 1

    def test_crash_after_ack_does_not_replay_event(
        self, prepared, fixtures, tmp_path, monkeypatch
    ):
        store, service, content, _, _ = prepared
        snapshot = _request(service, content)
        store.transition(
            content["plan_id"],
            content["plan_version"],
            "AWAITING_APPROVAL",
            "2026-09-14T08:01:00+08:00",
            approval_set_id=snapshot["approval_set_id"],
        )
        store.commit_new_version(
            _signed_content(fixtures, plan_version=2),
            "2026-09-14T08:02:00+08:00",
        )
        original = store.acknowledge_superseded

        def ack_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash after acknowledgement")

        monkeypatch.setattr(store, "acknowledge_superseded", ack_then_crash)
        with pytest.raises(RuntimeError, match="after acknowledgement"):
            service.consume_superseded("2026-09-14T08:03:00+08:00")
        assert store.pending_superseded() == []
        reloaded = ApprovalService(store, tmp_path / "approvals.json")
        assert reloaded.check_approval_status(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            NOW,
        )["aggregate_status"] == "INVALIDATED"

    def test_draft_lifecycle_set_is_invalidated_by_binding_fallback(
        self, prepared, fixtures
    ):
        store, service, content, _, _ = prepared
        snapshot = _request(service, content)
        for request in snapshot["approvals"]:
            snapshot = service.record_decision(
                request["approval_request_id"],
                "APPROVED",
                request["approver_role"],
                f"{request['approver_role']} authenticated",
                "2026-09-14T08:01:00+08:00",
            )
        assert snapshot["aggregate_status"] == "APPROVED"
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"

        store.commit_new_version(
            _signed_content(fixtures, plan_version=2),
            "2026-09-14T08:02:00+08:00",
        )
        event = store.pending_superseded()[0]
        assert event["approval_set_id"] is None

        assert service.consume_superseded("2026-09-14T08:03:00+08:00") == 1
        status = service.check_approval_status(
            snapshot["approval_set_id"],
            content["plan_id"],
            content["plan_version"],
            content["plan_digest"],
            "2026-09-14T08:03:00+08:00",
        )
        assert status["aggregate_status"] == "INVALIDATED"
        with pytest.raises(ApprovalSetInvalidatedError):
            service.require_approved(
                snapshot["approval_set_id"],
                content["plan_id"],
                content["plan_version"],
                content["plan_digest"],
                "2026-09-14T08:03:00+08:00",
            )

    def test_missing_named_set_is_not_acknowledged(self, fixtures, tmp_path):
        store = PlanStore()
        content = _signed_content(fixtures)
        store.put_content(content)
        store.create_lifecycle(
            content["plan_id"], content["plan_version"], content["plan_digest"], NOW
        )
        store.transition(
            content["plan_id"], 1, "AWAITING_APPROVAL", NOW, approval_set_id="APS-MISSING"
        )
        next_content = _signed_content(fixtures, plan_version=2)
        store.commit_new_version(next_content, "2026-09-14T08:02:00+08:00")
        with pytest.raises(ApprovalStateNotFoundError):
            ApprovalService(store, tmp_path / "empty.json").consume_superseded(
                "2026-09-14T08:03:00+08:00"
            )
        assert len(store.pending_superseded()) == 1
