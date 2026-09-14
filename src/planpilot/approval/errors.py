"""Approval failures with exact V1.8 framework error details."""

from __future__ import annotations

import copy

from planpilot.validation import validate


_DETAIL_SCHEMA_BY_CODE = {
    "APPROVAL_REQUIRED": "error_details_approval_required",
    "APPROVAL_REJECTED": "error_details_approval_rejected",
    "APPROVAL_EXPIRED": "error_details_approval_expired",
    "APPROVAL_WINDOW_CLOSED": "error_details_approval_window_closed",
    "APPROVAL_SET_INVALIDATED": "error_details_approval_set_invalidated",
    "APPROVAL_SET_INCOMPLETE": "error_details_approval_set_incomplete",
    "STATE_NOT_FOUND": "error_details_state_not_found",
}


class ApprovalError(Exception):
    """Base for non-retryable approval outcomes exposed by tool adapters."""

    code = "INTERNAL_ERROR"
    retryable = False

    def __init__(self, message: str, details: dict) -> None:
        super().__init__(message)
        self.message = message
        self.details = copy.deepcopy(details)
        schema = _DETAIL_SCHEMA_BY_CODE[self.code]
        validate(self.details, schema, schema)

    def to_error_details(self) -> dict:
        return copy.deepcopy(self.details)


class ApprovalRequiredError(ApprovalError):
    code = "APPROVAL_REQUIRED"

    def __init__(self, missing_actions: list[str], approver_roles: list[str]) -> None:
        super().__init__(
            f"approval is still required for: {', '.join(missing_actions)}",
            {"missing_actions": list(missing_actions), "approver_roles": list(approver_roles)},
        )


class ApprovalRejectedError(ApprovalError):
    code = "APPROVAL_REJECTED"

    def __init__(self, approval: dict) -> None:
        super().__init__(
            f"approval request {approval['approval_request_id']} was rejected",
            {
                "approval_request_id": approval["approval_request_id"],
                "action": approval["action"],
                "decision_reason": approval["decision_reason"],
                "decided_by": approval["decided_by"],
            },
        )


class ApprovalExpiredError(ApprovalError):
    code = "APPROVAL_EXPIRED"

    def __init__(self, approval: dict) -> None:
        super().__init__(
            f"approval request {approval['approval_request_id']} expired",
            {
                "approval_request_id": approval["approval_request_id"],
                "action": approval["action"],
                "expired_at": approval["expires_at"],
            },
        )


class ApprovalWindowClosedError(ApprovalError):
    code = "APPROVAL_WINDOW_CLOSED"

    def __init__(self, server_now: str, horizon_guard: str, minimum_ttl_seconds: int) -> None:
        super().__init__(
            "planning horizon leaves no legal minimum approval window",
            {
                "server_now": server_now,
                "horizon_guard": horizon_guard,
                "minimum_ttl_seconds": minimum_ttl_seconds,
            },
        )


class ApprovalSetInvalidatedError(ApprovalError):
    code = "APPROVAL_SET_INVALIDATED"

    def __init__(
        self,
        approval_set_id: str,
        invalidation_cause: str,
        superseded_plan_digest: str | None,
    ) -> None:
        super().__init__(
            f"approval set {approval_set_id} is invalidated",
            {
                "approval_set_id": approval_set_id,
                "invalidation_cause": invalidation_cause,
                "superseded_plan_digest": superseded_plan_digest,
            },
        )


class ApprovalSetIncompleteError(ApprovalError):
    code = "APPROVAL_SET_INCOMPLETE"

    def __init__(
        self,
        approval_set_id: str,
        missing_actions: list[str],
        required_count: int,
        present_count: int,
    ) -> None:
        super().__init__(
            f"approval set {approval_set_id} is not the complete required set",
            {
                "approval_set_id": approval_set_id,
                "missing_actions": list(missing_actions),
                "required_count": required_count,
                "present_count": present_count,
            },
        )


class ApprovalStateNotFoundError(ApprovalError):
    code = "STATE_NOT_FOUND"

    def __init__(self, approval_set_id: str) -> None:
        super().__init__(
            f"approval set {approval_set_id} was not found",
            {
                "lookup_kind": "approval_set",
                "requested_state_id": approval_set_id,
                "requested_plan_id": None,
            },
        )


class ApprovalInvariantError(Exception):
    """Caller or persisted-state defect with no truthful registered tool code."""


class ApprovalDecisionConflictError(ApprovalInvariantError):
    """A decided request was replayed with different decision content."""


__all__ = [
    "ApprovalError",
    "ApprovalRequiredError",
    "ApprovalRejectedError",
    "ApprovalExpiredError",
    "ApprovalWindowClosedError",
    "ApprovalSetInvalidatedError",
    "ApprovalSetIncompleteError",
    "ApprovalStateNotFoundError",
    "ApprovalInvariantError",
    "ApprovalDecisionConflictError",
]
