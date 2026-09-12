"""Store exceptions carrying contract-shaped error details.

Every exception here maps to exactly one code in
`tool_execution_contract.retryability_registry` and one schema in
`tool_execution_contract.details_schemas`. The `details` dict is built to satisfy
that schema at construction time, so the tool layer can emit a `tool_error`
without re-deriving or re-validating anything.

All three codes are non-retryable in the registry. That is asserted in
tests/unit/test_errors_schema.py rather than assumed here.

No new error code may be invented. If none of these fits, that is a contract
revision request, not a coding decision — see .kiro/steering/contract-authority.md
rule 3.
"""

from __future__ import annotations

__all__ = [
    "StoreError",
    "StoreInvariantError",
    "CanonicalizationError",
    "DigestMismatchError",
    "VersionConflictError",
    "IdempotencyConflictError",
    "TransitionNotAllowedError",
    "LifecycleAlreadyExistsError",
    "PlanNotFoundError",
    "RETRYABILITY",
]

# Mirror of tool_execution_contract.retryability_registry for the codes this
# module can raise. Tests assert this matches the contract exactly, so the copy
# cannot silently drift.
RETRYABILITY: dict[str, bool] = {
    "INVALID_INPUT": False,
    "STATE_NOT_FOUND": False,
    "PLAN_VERSION_CONFLICT": False,
    "PLAN_DIGEST_MISMATCH": False,
    "IDEMPOTENCY_CONFLICT": False,
}


class StoreError(Exception):
    """Base class. Carries a contract error code and schema-shaped details."""

    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, details: dict) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    @property
    def retryable(self) -> bool:
        return RETRYABILITY[self.code]

    def to_error_details(self) -> dict:
        """Return a copy, so a caller mutating it cannot corrupt this instance."""
        return dict(self.details)


class CanonicalizationError(StoreError):
    """Input cannot be canonically serialized.

    Raised for non-finite floats: `json.dumps` emits `NaN`/`Infinity` by default,
    which are not valid JSON and would make the digest unreproducible on any
    parser that rejects them. This is a silent reproducibility hole, so it is
    closed at the serializer rather than at the caller.

    Maps to INVALID_INPUT (the content handed to the store was not valid).

    details schema: $defs.error_details_invalid_input
      required: field_errors, rejected_entity_type, rejected_entity_id
      field_errors items: $defs.validation_issue
        required: code, severity, entity_type, entity_id, field, message
        code must come from $defs.validation_issue_code (INVALID_VALUE here)
    """

    code = "INVALID_INPUT"

    def __init__(self, message: str, json_path: str, entity_type: str = "plan_content",
                 entity_id: str | None = None) -> None:
        super().__init__(message, {
            "field_errors": [{
                "code": "INVALID_VALUE",
                "severity": "ERROR",
                "entity_type": entity_type,
                "entity_id": entity_id,
                "field": json_path,
                "message": message,
            }],
            "rejected_entity_type": entity_type,
            "rejected_entity_id": entity_id,
        })
        self.json_path = json_path


class DigestMismatchError(StoreError):
    """Stored/declared digest does not match the recomputed digest.

    This is the contract's defence against a tampered or corrupted plan reaching
    approval or publish — and, practically, against the known gateway failure mode
    where malformed tool-call XML once made an agent fabricate tool results. A
    fabricated plan cannot produce a matching digest.

    details schema: $defs.error_details_plan_digest_mismatch
      required: expected_plan_digest, recomputed_plan_digest
    """

    code = "PLAN_DIGEST_MISMATCH"

    def __init__(
        self,
        plan_id: str,
        plan_version: int,
        expected_plan_digest: str,
        recomputed_plan_digest: str,
    ) -> None:
        super().__init__(
            f"plan {plan_id} v{plan_version} digest mismatch: "
            f"declared {expected_plan_digest[:16]}… != recomputed {recomputed_plan_digest[:16]}…",
            {
                "expected_plan_digest": expected_plan_digest,
                "recomputed_plan_digest": recomputed_plan_digest,
            },
        )
        self.plan_id = plan_id
        self.plan_version = plan_version


class VersionConflictError(StoreError):
    """Optimistic concurrency control failed.

    details schema: $defs.error_details_plan_version_conflict
      required: expected_plan_version, actual_plan_version
    """

    code = "PLAN_VERSION_CONFLICT"

    def __init__(self, plan_id: str, expected_plan_version: int, actual_plan_version: int) -> None:
        super().__init__(
            f"plan {plan_id} version conflict: expected v{expected_plan_version}, "
            f"store has v{actual_plan_version}",
            {
                "expected_plan_version": expected_plan_version,
                "actual_plan_version": actual_plan_version,
            },
        )
        self.plan_id = plan_id


class IdempotencyConflictError(StoreError):
    """The same idempotency key was replayed with DIFFERENT content.

    The contract defines this code by exactly this shape — `plan_store`
    idempotency: "Repeating request_approval for the same binding and action
    returns the existing request unchanged; repeating it with a different action
    returns the same complete set." Applied to the write-once content store: the
    key is `(plan_id, plan_version)`, an identical rewrite is a no-op retry, and a
    differing rewrite is a conflict.

    details schema: $defs.error_details_idempotency_conflict
      required: idempotency_key, original_plan_id, original_status
    """

    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self, plan_id: str, plan_version: int, original_status: str | None = None) -> None:
        super().__init__(
            f"plan {plan_id} v{plan_version} already stored with different content; "
            f"write-once violated",
            {
                "idempotency_key": f"{plan_id}:{plan_version}",
                "original_plan_id": plan_id,
                "original_status": original_status,
            },
        )
        self.plan_id = plan_id
        self.plan_version = plan_version


class StoreInvariantError(Exception):
    """Base for caller programming errors that have NO registered contract code.

    Why this class exists: the contract registers 18 error codes, and none of
    them describes "the calling code asked for something structurally impossible".
    `VALIDATION_FAILED`'s details schema is the factory-state shape
    (status / errors / quarantined_entity_count). `POLICY_VIOLATION`'s
    `violated_policy` is a closed enum whose five values are all about materials,
    safety, untrusted data and publishing. `PLAN_VERSION_CONFLICT` means "you
    expected a different version". Forcing these conditions into any of those
    would misreport them, and inventing a new code is forbidden
    (.kiro/steering/contract-authority.md rule 3).

    So they surface as crashes. These are bugs in the caller, not runtime
    situations a planner could act on — there is no user action that fixes them,
    so they must never be rendered as a tool_error.

    Recorded as finding F-STORE-01 in the review handoff: if a legal runtime path
    ever needs to refuse one of these AND tell the user, the contract needs a new
    registered code. That is a contract revision, not something to solve here.
    """


class TransitionNotAllowedError(StoreInvariantError):
    """The caller requested a lifecycle transition the store cannot perform.

    Raised when the current status is terminal for this store (SUPERSEDED: its
    approvals are invalidated and it can never be published).

    The authoritative transition GRAPH is `workflow.transitions` (11 states);
    lifecycle status is a projection of it. This module deliberately does not
    re-implement that graph — doing so would create a second source of truth that
    can drift, which is the orphan-spec defect class the V1.8 review found eight
    instances of. Only terminality is enforced here.
    """

    def __init__(self, plan_id: str, plan_version: int, from_status: str, to_status: str) -> None:
        super().__init__(
            f"plan {plan_id} v{plan_version}: cannot transition {from_status} -> {to_status}; "
            f"{from_status} is terminal for the store"
        )
        self.plan_id = plan_id
        self.plan_version = plan_version
        self.from_status = from_status
        self.to_status = to_status


class LifecycleAlreadyExistsError(StoreInvariantError):
    """create_lifecycle was called for a (plan_id, plan_version) that has one.

    Lifecycle records are mutable, so an existing record may already have
    progressed past DRAFT. Re-creating it would silently rewind status and
    approval-set binding — which would break
    security_controls.approvals_bound_to_plan_version_and_digest. Refusing is
    correct; the caller should transition the existing record instead.
    """

    def __init__(self, plan_id: str, plan_version: int, existing_status: str) -> None:
        super().__init__(
            f"plan {plan_id} v{plan_version} already has a lifecycle record "
            f"(status={existing_status}); transition it instead of re-creating it"
        )
        self.plan_id = plan_id
        self.plan_version = plan_version
        self.existing_status = existing_status


class PlanNotFoundError(StoreError):
    """Requested plan/version is absent from the store.

    details schema: $defs.error_details_state_not_found
      required: lookup_kind, requested_state_id, requested_plan_id
    `requested_state_id` is null for plan lookups (it is the workflow-state field);
    both fields are required by the schema even when inapplicable, so they are
    always present.
    """

    code = "STATE_NOT_FOUND"

    def __init__(self, plan_id: str, plan_version: int | None = None, lookup_kind: str = "plan") -> None:
        detail = f" v{plan_version}" if plan_version is not None else " (latest)"
        super().__init__(
            f"no {lookup_kind} {plan_id}{detail} in store",
            {
                "lookup_kind": lookup_kind,
                "requested_state_id": None,
                "requested_plan_id": plan_id,
            },
        )
        self.plan_id = plan_id
        self.plan_version = plan_version
