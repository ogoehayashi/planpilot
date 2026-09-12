"""Immutable plan content + mutable lifecycle store.

See .kiro/specs/plan-store-and-digest/design.md.

Dependency rule: nothing in this package may import from planpilot.inference.
The digest is computed with no LLM in the loop — that is what makes
provider_portability true and what keeps a fabricated tool result from passing.
"""

from .digest import (
    DIGEST_EXCLUDED_FIELDS,
    ENGINE_DIGEST_EXCLUDED_FIELDS,
    OPERATION_SORT_KEY_FIELDS,
    assert_digest_consistent,
    canonical_json,
    canonical_plan_digest,
    sort_operations,
)
from .errors import (
    RETRYABILITY,
    CanonicalizationError,
    DigestMismatchError,
    IdempotencyConflictError,
    InvalidContentError,
    LifecycleAlreadyExistsError,
    PlanNotFoundError,
    SchemaViolationError,
    StoreError,
    StoreInvariantError,
    TransitionNotAllowedError,
    VersionConflictError,
)
from .plan_store import INVALIDATION_CAUSES, LIFECYCLE_STATUSES, PlanStore

__all__ = [
    "DIGEST_EXCLUDED_FIELDS",
    "ENGINE_DIGEST_EXCLUDED_FIELDS",
    "OPERATION_SORT_KEY_FIELDS",
    "assert_digest_consistent",
    "canonical_json",
    "canonical_plan_digest",
    "sort_operations",
    "RETRYABILITY",
    "CanonicalizationError",
    "DigestMismatchError",
    "IdempotencyConflictError",
    "InvalidContentError",
    "LifecycleAlreadyExistsError",
    "PlanNotFoundError",
    "SchemaViolationError",
    "StoreError",
    "StoreInvariantError",
    "TransitionNotAllowedError",
    "VersionConflictError",
    "INVALIDATION_CAUSES",
    "LIFECYCLE_STATUSES",
    "PlanStore",
]
