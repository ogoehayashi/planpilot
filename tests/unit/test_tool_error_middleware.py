from __future__ import annotations

import json
import uuid

import pytest

from planpilot.approval.errors import (
    ApprovalDecisionConflictError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalRequiredError,
    ApprovalSetIncompleteError,
    ApprovalSetInvalidatedError,
    ApprovalStateNotFoundError,
    ApprovalWindowClosedError,
)
from planpilot.store.errors import (
    CanonicalizationError,
    DigestMismatchError,
    IdempotencyConflictError,
    PlanNotFoundError,
    StoreInvariantError,
    VersionConflictError,
)
from planpilot.tools import (
    DeadlineContext,
    ExecutionContext,
    FrameworkDomainError,
    ToolErrorMiddleware,
)
from planpilot.tools.correlation import valid_uuid4
from planpilot.tools.registry import error_specs, tool_names
from planpilot.validation import contract_path, is_tool_payload_valid


UUID = "12345678-1234-4234-9234-123456789abc"
INPUT = {"workbook_path": "factory.xlsx", "as_of_time": "2026-09-16T09:00:00+08:00"}
OUTPUT = {"state_id": "STATE-1", "dataset_version": "2.0", "entity_counts": {}, "warnings": []}


def middleware(**kwargs):
    return ToolErrorMiddleware(uuid_factory=lambda: uuid.UUID(UUID), **kwargs)


def test_registry_is_derived_from_exact_public_contract():
    assert tool_names() == (
        "load_factory_state", "validate_factory_state", "generate_plan_options",
        "validate_plan", "request_approval", "check_approval_status",
        "publish_plan", "log_security_event",
    )
    assert len(error_specs()) == 18
    assert {code for code, spec in error_specs().items() if spec.retryable} == {
        "DEADLINE_EXCEEDED", "RATE_LIMITED", "INTERNAL_ERROR"
    }


def test_contract_registry_refs_failure_schemas_and_correlation_pattern_are_pinned():
    contract = json.loads(contract_path().read_text(encoding="utf-8"))
    expected_codes = (
        "INVALID_INPUT", "STATE_NOT_FOUND", "VALIDATION_FAILED", "NO_FEASIBLE_PLAN",
        "DEADLINE_EXCEEDED", "SEARCH_ESCALATION_EXHAUSTED", "RATE_LIMITED",
        "APPROVAL_REQUIRED", "APPROVAL_REJECTED", "APPROVAL_EXPIRED",
        "APPROVAL_WINDOW_CLOSED", "APPROVAL_SET_INVALIDATED", "APPROVAL_SET_INCOMPLETE",
        "PLAN_VERSION_CONFLICT", "PLAN_DIGEST_MISMATCH", "IDEMPOTENCY_CONFLICT",
        "POLICY_VIOLATION", "INTERNAL_ERROR",
    )
    execution = contract["tool_execution_contract"]
    assert tuple(execution["retryability_registry"]) == expected_codes
    assert tuple(execution["details_schemas"]) == expected_codes
    assert {
        code: spec.details_def for code, spec in error_specs().items()
    } == {
        code: reference["$ref"].rsplit("/", 1)[-1]
        for code, reference in execution["details_schemas"].items()
    }
    assert all(tool["failure_schema"] == {"$ref": "#/$defs/tool_error"} for tool in contract["tools"])
    assert contract["$defs"]["tool_error"]["properties"]["correlation_id"]["pattern"] == (
        "^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$"
    )
    assert set(execution["retry_policies"]) == {
        "DEADLINE_EXCEEDED", "RATE_LIMITED", "INTERNAL_ERROR"
    }


def test_success_is_validated_serialized_then_committed_once():
    trace = []

    class Call:
        def prepare(self, payload, context):
            trace.append(("prepare", payload, context.correlation_id))
            return OUTPUT

        def commit(self):
            trace.append(("commit",))

        def rollback(self):
            trace.append(("rollback",))

    result = middleware().execute("load_factory_state", INPUT, Call())
    assert result.is_error is False
    assert json.loads(result.wire_bytes) == OUTPUT
    assert is_tool_payload_valid(json.loads(result.wire_bytes), "load_factory_state", "output_schema")
    assert [step[0] for step in trace] == ["prepare", "commit"]
    with pytest.raises(TypeError):
        result.payload["state_id"] = "changed"


def test_input_rejection_never_invokes_handler_and_is_contract_valid():
    invoked = 0

    def handler(payload, context):
        nonlocal invoked
        invoked += 1
        return OUTPUT

    result = middleware().execute("load_factory_state", {"workbook_path": "x"}, handler)
    assert invoked == 0
    assert result.payload["error_code"] == "INVALID_INPUT"
    assert result.payload["retryable"] is False
    assert len(result.wire_bytes) <= 4096
    assert is_tool_payload_valid(json.loads(result.wire_bytes), "load_factory_state", "failure_schema")


def test_registered_domain_failure_owns_retryability_and_required_details():
    def handler(payload, context):
        raise FrameworkDomainError(
            "RATE_LIMITED", "Request rate exceeded.",
            {"retry_after_seconds": 3, "limit_scope": "tool"},
        )

    result = middleware().execute("load_factory_state", INPUT, handler)
    assert result.payload["error_code"] == "RATE_LIMITED"
    assert result.payload["retryable"] is True
    with pytest.raises(Exception):
        FrameworkDomainError("RATE_LIMITED", "bad", {"retry_after_seconds": 1})


def test_spoofed_and_unknown_exceptions_are_sanitized():
    class Spoof(Exception):
        code = "RATE_LIMITED"
        details = {"retry_after_seconds": 1, "limit_scope": "tool"}

    spoofed = middleware().execute(
        "load_factory_state", INPUT,
        lambda payload, context: (_ for _ in ()).throw(Spoof("ordinary spoof")),
    )
    assert spoofed.payload["error_code"] == "INTERNAL_ERROR"

    secret = "Bearer super-secret-token /Users/alice/private.txt"
    result = middleware().execute(
        "load_factory_state", INPUT,
        lambda payload, context: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    text = result.wire_bytes.decode("utf-8")
    assert result.payload["error_code"] == "INTERNAL_ERROR"
    assert result.payload["retryable"] is True
    assert "super-secret-token" not in text and "/Users/alice" not in text


def test_oversize_multibyte_failure_falls_back_within_exact_wire_limit():
    huge = "🙂" * 500
    result = middleware().execute(
        "load_factory_state", INPUT,
        lambda payload, context: (_ for _ in ()).throw(
            FrameworkDomainError(
                "INTERNAL_ERROR", huge[:500],
                {"diagnostic_class": "Huge", "safe_detail": huge[:500]},
            )
        ),
    )
    assert result.payload["error_code"] == "INTERNAL_ERROR"
    assert result.payload["details"]["diagnostic_class"] == "ToolMiddlewareFailure"
    assert len(result.wire_bytes) <= 4096
    assert json.loads(result.wire_bytes.decode("utf-8"))["error_code"] == result.payload["error_code"]


@pytest.mark.parametrize("candidate", [
    {"state_id": "missing-fields"},
    {"state_id": "S", "dataset_version": "2", "entity_counts": {"bad": float("nan")}, "warnings": []},
])
def test_invalid_or_non_json_output_rolls_back_without_commit(candidate):
    trace = []

    class Call:
        def prepare(self, payload, context):
            return candidate
        def commit(self):
            trace.append("commit")
        def rollback(self):
            trace.append("rollback")

    result = middleware().execute("load_factory_state", INPUT, Call())
    assert result.payload["error_code"] == "INTERNAL_ERROR"
    assert trace == ["rollback"]


def test_commit_failure_rolls_back_and_never_exposes_staged_state():
    visible = []

    class Call:
        def prepare(self, payload, context):
            self.staged = "STATE-1"
            return OUTPUT
        def commit(self):
            raise OSError("disk path and secret")
        def rollback(self):
            self.staged = None

    call = Call()
    result = middleware().execute("load_factory_state", INPUT, call)
    assert result.payload["error_code"] == "INTERNAL_ERROR"
    assert call.staged is None and visible == []


def test_existing_valid_correlation_is_reused_and_invalid_context_fails_closed():
    ok = middleware().execute(
        "load_factory_state", INPUT, lambda payload, context: OUTPUT,
        context=ExecutionContext(UUID),
    )
    assert ok.correlation_id == UUID and valid_uuid4(ok.correlation_id)
    called = False

    def handler(payload, context):
        nonlocal called
        called = True
        return OUTPUT

    bad = middleware().execute(
        "load_factory_state", INPUT, handler, context=ExecutionContext("corr-1")
    )
    assert not called
    assert bad.payload["error_code"] == "INTERNAL_ERROR"
    assert bad.correlation_id == UUID


def test_deadline_is_checked_before_and_after_prepare_with_generation_progress():
    now = iter([0.0, 11.0])
    pre = middleware(clock=lambda: next(now)).execute(
        "load_factory_state", INPUT, lambda payload, context: OUTPUT,
        deadline=DeadlineContext("load_and_input_validation", 5.0, 0.0),
    )
    assert pre.payload["error_code"] == "DEADLINE_EXCEEDED"
    assert "profiles_completed" not in pre.payload["details"]

    ticks = iter([0.0, 1.0, 5.0])
    generation_input = {
        "state_id": "S", "profiles": ["Balanced"], "event_id": None,
        "baseline_plan_id": None, "max_runtime_seconds": 40,
    }
    post = middleware(clock=lambda: next(ticks)).execute(
        "generate_plan_options", generation_input, lambda payload, context: {
            "plan_options": [], "stored_plan_count": 0,
            "solver_runtime_seconds": 0, "per_profile_runtime_seconds": {},
        },
        deadline=DeadlineContext("plan_generation", 3.0, 0.0, ("Balanced",)),
    )
    assert post.payload["error_code"] == "DEADLINE_EXCEEDED"
    assert post.payload["details"]["profiles_completed"] == ("Balanced",)


def test_observer_runs_once_after_outcome_and_cannot_rewrite_it():
    events = []

    def observer(event):
        events.append(event)
        raise RuntimeError("observer unavailable")

    result = middleware(observer=observer, clock=lambda: 1.0).execute(
        "load_factory_state", INPUT, lambda payload, context: OUTPUT,
        deadline=DeadlineContext("load_and_input_validation", 10.0, 0.0),
        entity_references={"state_id": "STATE-1"},
    )
    assert result.is_error is False
    assert len(events) == 1
    assert events[0]["committed"] is True
    assert events[0]["elapsed_seconds"] >= 0
    assert events[0]["deadline"]["stage"] == "load_and_input_validation"
    assert events[0]["entity_references"]["state_id"] == "STATE-1"


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_process_control_exceptions_propagate_and_rollback(signal):
    rolled_back = []

    class Call:
        def prepare(self, payload, context):
            raise signal()
        def commit(self):
            raise AssertionError("unreachable")
        def rollback(self):
            rolled_back.append(True)

    with pytest.raises(signal):
        middleware().execute("load_factory_state", INPUT, Call())
    assert rolled_back == [True]


def test_unknown_tool_is_rejected_before_execution():
    with pytest.raises(KeyError):
        middleware().execute("nineteenth_tool", {}, lambda payload, context: {})


@pytest.mark.parametrize("exc,code", [
    (CanonicalizationError("bad numeric value", "$.value"), "INVALID_INPUT"),
    (PlanNotFoundError("PLAN-1", 1), "STATE_NOT_FOUND"),
    (VersionConflictError("PLAN-1", 1, 2), "PLAN_VERSION_CONFLICT"),
    (DigestMismatchError("PLAN-1", 1, "a" * 64, "b" * 64), "PLAN_DIGEST_MISMATCH"),
    (IdempotencyConflictError("PLAN-1", 1, "DRAFT"), "IDEMPOTENCY_CONFLICT"),
])
def test_existing_store_errors_round_trip_defensively(exc, code):
    original = exc.to_error_details()
    result = middleware().execute(
        "load_factory_state", INPUT,
        lambda payload, context: (_ for _ in ()).throw(exc),
    )
    assert result.payload["error_code"] == code
    assert json.loads(result.wire_bytes)["details"] == original
    original["attacker"] = "mutation"
    assert "attacker" not in result.payload["details"]


@pytest.mark.parametrize("exc,code", [
    (ApprovalRequiredError(["publish_plan"], ["Production Manager"]), "APPROVAL_REQUIRED"),
    (ApprovalRejectedError({
        "approval_request_id": "APR-1", "action": "publish_plan",
        "decision_reason": None, "decided_by": "manager@example.test",
    }), "APPROVAL_REJECTED"),
    (ApprovalExpiredError({
        "approval_request_id": "APR-1", "action": "publish_plan",
        "expires_at": "2026-09-16T10:00:00+08:00",
    }), "APPROVAL_EXPIRED"),
    (ApprovalWindowClosedError(
        "2026-09-16T09:00:00+08:00", "2026-09-16T09:05:00+08:00", 600,
    ), "APPROVAL_WINDOW_CLOSED"),
    (ApprovalSetInvalidatedError("SET-1", "digest_changed", "a" * 64), "APPROVAL_SET_INVALIDATED"),
    (ApprovalSetIncompleteError("SET-1", ["publish_plan"], 2, 1), "APPROVAL_SET_INCOMPLETE"),
    (ApprovalStateNotFoundError("SET-1"), "STATE_NOT_FOUND"),
])
def test_existing_approval_errors_round_trip(exc, code):
    result = middleware().execute(
        "load_factory_state", INPUT,
        lambda payload, context: (_ for _ in ()).throw(exc),
    )
    assert result.payload["error_code"] == code
    assert is_tool_payload_valid(json.loads(result.wire_bytes), "load_factory_state", "failure_schema")


@pytest.mark.parametrize("exc", [StoreInvariantError("store bug"), ApprovalDecisionConflictError("approval bug")])
def test_foundation_invariants_are_server_faults(exc):
    result = middleware().execute(
        "load_factory_state", INPUT,
        lambda payload, context: (_ for _ in ()).throw(exc),
    )
    assert result.payload["error_code"] == "INTERNAL_ERROR"
    assert "bug" not in result.wire_bytes.decode("utf-8")


def test_solver_exhaustion_is_preserved_and_handler_runs_once():
    calls = 0
    contract = json.loads(contract_path().read_text(encoding="utf-8"))
    exhaustion_reason = contract["$defs"]["error_details_search_escalation_exhausted"][
        "properties"
    ]["last_failure"]["enum"][1]

    def handler(payload, context):
        nonlocal calls
        calls += 1
        raise FrameworkDomainError("SEARCH_ESCALATION_EXHAUSTED", "Search ladder exhausted.", {
            "stage": "plan_generation", "total_elapsed_seconds": 40,
            "total_budget_seconds": 40,
            "rungs_attempted": ["CP-SAT", "PRIORITY_DISPATCH_FALLBACK"],
            "last_failure": exhaustion_reason,
        })

    result = middleware().execute("load_factory_state", INPUT, handler)
    assert calls == 1
    assert result.payload["error_code"] == "SEARCH_ESCALATION_EXHAUSTED"
    assert result.payload["retryable"] is False
