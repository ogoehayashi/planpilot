"""Schemas for the shapes `plan_store` owns but the contract does not define.

The contract defines `$defs.plan_content` and `$defs.plan_lifecycle`, and the
store validates against both through `planpilot.validation`. Two shapes are left
over that the store itself invented:

* the persistence envelope written by `dump_state` / read by `load_state`
* the durable supersede outbox read through `pending_superseded` and acknowledged
  through `acknowledge_superseded`

`load_state` originally restored both with no checks at all — audit finding P1. A
hand-edited dump could carry a duplicate `(plan_id, plan_version)` key, which the
dict comprehension silently collapsed to the last occurrence, and a fabricated
supersede event pointing at a plan that never existed, which the approval service
would then act on by invalidating a real approval set.

These are module-owned schemas, so they are NOT registered in the contract and
are validated with `validate_shape`. Anything the contract DOES define is left to
the contract's own `$defs` — duplicating `$defs.plan_lifecycle` here would create
a second source of truth that can drift, which is the same mistake the
closed-vocabulary guard exists to prevent.

Field-level constraints that the contract pins are mirrored here on purpose and
pinned by test, so a drift fails loudly:
* digest pattern `^[a-f0-9]{64}$`  — $defs.plan_content.properties.plan_digest
* `invalidation_cause` enum        — $defs.error_details_approval_set_invalidated
* `updated_at` / `superseded_at` as date-time — $defs.plan_lifecycle
* lifecycle `status` enum          — $defs.plan_lifecycle.properties.status.enum
"""

from __future__ import annotations

__all__ = ["STATE_SCHEMA", "SUPERSEDED_EVENT_SCHEMA", "SUPERSEDED_ACK_SCHEMA"]

# Mirrors $defs.error_details_approval_set_invalidated.properties.invalidation_cause.
# Must stay equal to plan_store.INVALIDATION_CAUSES; pinned by test.
_INVALIDATION_CAUSES = [
    "plan_regenerated",
    "plan_content_mutated",
    "kpi_changed",
    "digest_changed",
    "superseded_version",
]

# Mirrors $defs.plan_lifecycle.properties.status.enum.
_LIFECYCLE_STATUSES = [
    "DRAFT",
    "PROPOSED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "PUBLISHED",
    "BLOCKED",
    "SUPERSEDED",
]

# A lifecycle record as persisted. Note this is a SHAPE check only — the
# cross-record integrity rules (does plan_digest match the content it binds to?)
# are enforced separately in load_state, because no JSON Schema can express a
# relationship between two array elements keyed by a tuple.
_LIFECYCLE_SHAPE = {
    "type": "object",
    "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "plan_version": {"type": "integer", "minimum": 0},
        "plan_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "status": {"enum": _LIFECYCLE_STATUSES},
        "approval_set_id": {"type": ["string", "null"]},
        "published_version": {"type": ["integer", "null"], "minimum": 0},
        "updated_at": {"type": "string", "format": "date-time"},
    },
    "required": [
        "plan_id", "plan_version", "plan_digest", "status",
        "approval_set_id", "published_version", "updated_at",
    ],
    "additionalProperties": False,
}

SUPERSEDED_EVENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "plan_version": {"type": "integer", "minimum": 0},
        "plan_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "approval_set_id": {"type": ["string", "null"]},
        "superseded_at": {"type": "string", "format": "date-time"},
        "invalidation_cause": {"enum": _INVALIDATION_CAUSES},
    },
    "required": [
        "plan_id", "plan_version", "plan_digest", "approval_set_id",
        "superseded_at", "invalidation_cause",
    ],
    "additionalProperties": False,
}

# Delivery acknowledgement for the durable supersede outbox.  It is deliberately
# separate from SUPERSEDED_EVENT_SCHEMA: the event is immutable audit history,
# while acknowledgement is mutable delivery state.  Combining them would make an
# acknowledgement rewrite the audit record that the retention promise says to
# keep.
SUPERSEDED_ACK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "plan_version": {"type": "integer", "minimum": 0},
        "plan_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "approval_set_id": {"type": ["string", "null"]},
        "acknowledged_at": {"type": "string", "format": "date-time"},
    },
    "required": [
        "plan_id", "plan_version", "plan_digest", "approval_set_id",
        "acknowledged_at",
    ],
    "additionalProperties": False,
}

STATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        # plan_content records are validated against the contract's own
        # $defs.plan_content in load_state, not against a copy here. Only the
        # container shape is described.
        "content": {"type": "array", "items": {"type": "object"}},
        "lifecycle": {"type": "array", "items": _LIFECYCLE_SHAPE},
        "superseded_events": {
            "type": "array",
            "items": SUPERSEDED_EVENT_SCHEMA,
        },
        "superseded_acks": {
            "type": "array",
            "items": SUPERSEDED_ACK_SCHEMA,
        },
    },
    # superseded_acks was added after the first evidence format.  load_state()
    # treats an omitted list as empty so an old, otherwise-valid dump remains
    # readable; every newly written dump includes it.
    "required": ["content", "lifecycle", "superseded_events"],
    # Closed on purpose: an unknown top-level key means the dump was not written
    # by dump_state, so loading it would be trusting a file of unknown origin.
    "additionalProperties": False,
}
