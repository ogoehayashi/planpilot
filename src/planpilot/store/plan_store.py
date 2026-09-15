"""PlanStore: immutable plan_content + mutable plan_lifecycle.

Contract source — plan_store:
  records.plan_content    "immutable, content-addressed by plan_digest; written
                           once per version"
  records.plan_lifecycle  "mutable status, approval-set binding, published
                           version and updated_at"
  versioning              "plan_version increments on every regeneration for the
                           same horizon; a superseded version moves to
                           lifecycle.status=SUPERSEDED and its approval sets are
                           invalidated."
  retention               "Every version and its audit trail is retained for the
                           hackathon evidence pack; digests are recomputable from
                           stored content at any time."

Design decisions (see .kiro/specs/plan-store-and-digest/design.md §3.3):

* The clock is INJECTED. Every mutating method takes `ts`. A store that reads
  datetime.now() internally cannot be tested deterministically and would make
  the evidence pack unreproducible.

* Writes are validated on WRITE, not on read — by THREE gates, not one. A corrupt
  or fabricated plan never enters the store. This is the defence against the
  known gateway failure mode (starter-kit proxy.py: malformed tool-call XML once
  made an agent fabricate tool results).

  The first version of this module claimed digest verification alone was that
  defence. Audit finding P0-1 falsified it: the digest is content-addressed, so a
  caller who fabricates a plan can simply re-sign it, and a digest-consistent
  plan still violated `additionalProperties: false`, dropped required fields, and
  used `plan_version=True`. Digest consistency proves content was not altered
  after signing; only contract schema validation proves the content is legal.
  Both gates are required, and the third (key pre-flight) protects dict-key
  integrity, since `hash(True) == hash(1)`.

* Supersede records a fact; it does NOT mutate approval sets. This module does
  not own them. Reaching into another module's records is how the orphan-spec
  defect class happens — eight instances were found in the V1.8 review. The
  approval service reads `pending_superseded()`, invalidates idempotently, then
  calls `acknowledge_superseded()`.

* The full workflow graph is not copied here. The store does enforce local facts
  that must hold under every workflow: approval-bearing states are active-only,
  publication follows approval with an exact version binding, SUPERSEDED is
  terminal, and PUBLISHED may only be retired to SUPERSEDED by replanning.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .digest import (
    _total_key,
    assert_digest_consistent,
    canonical_json,
    canonical_plan_digest,
)
from .errors import (
    DigestMismatchError,
    IdempotencyConflictError,
    InvalidContentError,
    LifecycleAlreadyExistsError,
    PlanNotFoundError,
    SchemaViolationError,
    StoreInvariantError,
    TransitionNotAllowedError,
    VersionConflictError,
    VersionRouteError,
)
from .persistence_schema import STATE_SCHEMA, SUPERSEDED_ACK_SCHEMA
from ..validation import (
    SchemaValidationError,
    validate as validate_against_contract,
    validate_shape as validate_shape_against_schema,
)

__all__ = ["PlanStore", "LIFECYCLE_STATUSES", "INVALIDATION_CAUSES", "CREATION_STATUS"]

# $defs.plan_lifecycle.properties.status.enum — mirrored so the store can reject
# an unknown status without a jsonschema round trip. test_plan_store_invariants
# asserts this list equals the contract enum, so it cannot drift silently.
LIFECYCLE_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "PROPOSED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "PUBLISHED",
    "BLOCKED",
    "SUPERSEDED",
)

# SUPERSEDED can never be revived. PUBLISHED is a resting state rather than an
# absolute terminal: contract.workflow.transitions allows replanning, and the
# old published content version must then move to SUPERSEDED. Its only permitted
# successor is therefore SUPERSEDED.
_TERMINAL = frozenset({"SUPERSEDED"})

# These states may exist only on the active version.  AWAITING_APPROVAL does not
# itself grant production authority, but it creates an approval-set binding.  If
# it were allowed on a stale version, regeneration could be followed by opening a
# fresh approval set for the version that regeneration just retired.
_ACTIVE_ONLY_STATUSES = frozenset({"AWAITING_APPROVAL", "APPROVED", "PUBLISHED"})

# The only status create_lifecycle() may write (audit finding P0-bis).
#
# A named constant rather than a literal at the call site for two reasons. It
# documents the invariant where the other status sets live, and it gives the
# negative control a single-line mutation to remove: substituting a caller-
# supplied status here reopens the authority bypass, and the suite must notice.
#
# DRAFT specifically, not "the first status in the enum": the contract's
# workflow.transitions graph starts at DRAFT, and any other choice would be a
# second copy of that graph — the orphan-spec defect class the V1.8 review found
# eight instances of.
CREATION_STATUS = "DRAFT"

# Sort key for superseded_events in dump_state(), so the dump does not depend on
# insertion order (see that method for the defect this closes).
#
# Reuses digest._total_key rather than re-declaring a rank table: that helper is
# the F12 fix for exactly this problem — a sort key that must be TOTAL and must
# not raise when a nullable field meets a string one. A second copy of the rank
# table would be a second source of truth free to drift, which is the
# orphan-spec defect class.
#
# Every field of the event participates, so two distinct events can never tie
# and fall back to input order. plan_digest and superseded_at are strings and
# approval_set_id is string|null, which is the pair that needs the type rank.
_EVENT_SORT_FIELDS: tuple[str, ...] = (
    "plan_id",
    "plan_version",
    "plan_digest",
    "approval_set_id",
    "superseded_at",
    "invalidation_cause",
)

_ACK_SORT_FIELDS: tuple[str, ...] = (
    "plan_id",
    "plan_version",
    "plan_digest",
    "approval_set_id",
    "acknowledged_at",
)


def _event_sort_key(event: dict) -> tuple:
    """A total, insertion-order-independent key for one supersede event."""
    return tuple(_total_key(event.get(field)) for field in _EVENT_SORT_FIELDS)


def _ack_sort_key(ack: dict) -> tuple:
    """A total, insertion-order-independent key for one delivery acknowledgement."""
    return tuple(_total_key(ack.get(field)) for field in _ACK_SORT_FIELDS)


# $defs.error_details_approval_set_invalidated.properties.invalidation_cause.enum
# — mirrored, so a supersede event can only carry a registered cause. Pinned by
# test_plan_store_invariants like LIFECYCLE_STATUSES, so it cannot drift.
#
# The contract's workflow.approval_invalidation says regeneration "atomically
# invalidates the prior approval set", and approval_set_lifecycle.invalidation
# repeats it. A supersede event without a cause would leave the approval service
# guessing which of those five it is, so the event carries one.
INVALIDATION_CAUSES: tuple[str, ...] = (
    "plan_regenerated",
    "plan_content_mutated",
    "kpi_changed",
    "digest_changed",
    "superseded_version",
)

# The cause a superseded version carries. plan_store.versioning is the authority:
# "a superseded version moves to lifecycle.status=SUPERSEDED and its approval
# sets are invalidated".
_CAUSE_SUPERSEDED = "superseded_version"

# Supersede events are dumped and reloaded, and the contract defines no $defs
# entry for them, so this module owns their shape. Closed on purpose: an
# unvalidated event list is how a hand-edited dump injects a fabricated
# invalidation (audit finding P1).
_EVENT_KEYS = frozenset(
    {"plan_id", "plan_version", "plan_digest", "approval_set_id",
     "superseded_at", "invalidation_cause"}
)


class PlanStore:
    """In-memory store of record, with canonical JSON persistence.

    Keys are `(plan_id, plan_version)`. `plan_version` increments on every
    regeneration for the same horizon per plan_store.versioning.
    """

    def __init__(self) -> None:
        self._content: dict[tuple[str, int], dict] = {}
        self._lifecycle: dict[tuple[str, int], dict] = {}
        # Append-only audit history.  Delivery state is kept separately in
        # _superseded_acks so acknowledging a message never erases evidence.
        self._superseded: list[dict] = []
        self._superseded_acks: dict[tuple[str, int], dict] = {}

    # ------------------------------------------------------------------ write

    def put_content(self, content: dict) -> str:
        """Store immutable plan content. Write-once per (plan_id, plan_version).

        Writes the FIRST version of a plan. Adding a later version raises
        VersionRouteError — use commit_new_version() for that (audit finding
        P1-b, explained on that error class).

        Four independent gates, in this order:

        1. key pre-flight — plan_id/plan_version must be a str and a non-bool int
        2. **schema validation against `$defs.plan_content`** (audit finding P0-1)
        3. both digest identities must hold
        4. the plan must not already have versions (audit finding P1-b)

        Gates 2 and 3 are NOT redundant, and treating them as one check is exactly
        the P0-1 defect. The digest is content-addressed, so a caller who mutates
        content can always re-sign it: digest consistency proves the content was
        not tampered with AFTER signing, and never proves the content is legal.
        Before gate 2 existed, a plan violating `additionalProperties: false`, one
        missing a required field, and one with `plan_version=True` were all
        accepted as long as the digest matched.

        Idempotent for identical bytes: a retried write must not fail. Raising on
        an identical rewrite would make every retry path a bug.

        Returns the verified digest.
        """
        return self._store_content(content)

    def _store_content(self, content: dict, *, allow_new_version: bool = False) -> str:
        """The write path. `allow_new_version` is private on purpose.

        Only commit_new_version() passes it, because only that method performs the
        rest of the version transition (continuity, lifecycle, supersede,
        approval-invalidation event) atomically. Exposing the flag would recreate
        the P1-b bypass with an extra argument.
        """
        if not isinstance(content, dict):
            raise TypeError(f"content must be a dict, got {type(content).__name__}")

        plan_id = content.get("plan_id")
        plan_version = content.get("plan_version")
        # bool BEFORE int: isinstance(True, int) is True in Python, and
        # hash(True) == hash(1), so a bool version would collide with version 1
        # as a dict key and silently overwrite or read the wrong record. That is
        # a key-integrity hazard independent of the schema, which is why this
        # check stays even though $defs.plan_content also rejects True.
        if (
            not isinstance(plan_id, str)
            or not isinstance(plan_version, int)
            or isinstance(plan_version, bool)
        ):
            # Malformed content is structurally invalid, NOT a lookup failure.
            # STATE_NOT_FOUND would be the wrong code (nothing was looked up);
            # "plan_content_key" was never in the lookup_kind enum anyway (F2).
            raise InvalidContentError(
                str(plan_id), plan_version if isinstance(plan_version, int) else -1,
                json_path="plan_id" if not isinstance(plan_id, str) else "plan_version",
                reason=f"plan_id/plan_version must be str/non-bool int, got "
                       f"{type(plan_id).__name__}/{type(plan_version).__name__}",
            )

        # Gate 2: the content must satisfy the contract, not merely hash to itself.
        # Validated through the production entry point (planpilot.validation), the
        # same one the tests call — never a test-only validator.
        self._validate_record(content, "plan_content", plan_id)

        # Gate 3: verify before storing. Raises DigestMismatchError with
        # contract-shaped details if either identity fails.
        digest = assert_digest_consistent(content)

        key = (plan_id, plan_version)
        if key in self._content:
            existing = self._content[key]
            # Compare DIGESTS, not canonical_json. The digest sorts operations
            # (canonical_serialization), canonical_json does not — so two plans
            # differing only in operation order hash identically and ARE the same
            # content. Comparing canonical_json would wrongly raise an idempotency
            # conflict on a reordered-but-equivalent retry (audit F3).
            if existing["plan_digest"] == digest:
                return digest  # identical content: idempotent retry
            # Same key, different content: exactly what IDEMPOTENCY_CONFLICT is
            # registered for. PLAN_VERSION_CONFLICT would be wrong here — it
            # means "you expected another version", and expected==actual here.
            raise IdempotencyConflictError(
                plan_id=plan_id,
                plan_version=plan_version,
                original_status=(self._lifecycle.get(key) or {}).get("status"),
            )

        # Gate 4: this key is new, so if the plan already has versions this write
        # would ADD one. Refuse unless the caller is commit_new_version().
        #
        # Checked AFTER the idempotency branch above, so a retry of an already
        # stored version stays idempotent — gate 4 only fires on a genuinely new
        # key. Without it, current_active_version() (the max stored version)
        # would move to the new one while the previous version kept its APPROVED
        # status and its live approval set.
        if not allow_new_version:
            existing_versions = sorted(v for (pid, v) in self._content if pid == plan_id)
            if existing_versions:
                raise VersionRouteError(plan_id, plan_version, existing_versions)

        # Deep copy on the way in and out, so a caller mutating its dict cannot
        # change stored state (and vice versa). Immutability is enforced, not
        # merely documented.
        self._content[key] = copy.deepcopy(content)
        return digest

    def create_lifecycle(
        self,
        plan_id: str,
        plan_version: int,
        plan_digest: str,
        ts: str,
    ) -> dict:
        """Create the mutable lifecycle record for an already-stored content version.

        Always DRAFT, and that is a security property rather than a convenient
        default (audit finding P0-bis). This signature used to accept `status`,
        `approval_set_id` and `published_version`, so a caller could mint
        authority without ever reaching transition()'s gates:

            v1 and v2 stored, current_active_version() == 2
            create_lifecycle(v1, status="PUBLISHED", published_version=1)
            -> ACCEPTED

        transition() refused the equivalent move with VersionConflictError, so
        this bypassed an existing defence instead of covering an unimplemented
        one — and it was wider than the stale-version case, since PUBLISHED could
        be created for the active version too, letting the store manufacture
        publication authority through no gate at all.

        The record was schema-legal, which is why P0-1's contract validation
        could not see it: contract validity and state invariants are different
        questions, and satisfying the first says nothing about the second.

        APPROVED and PUBLISHED are reachable only through transition(), which
        compares against current_active_version(); SUPERSEDED only through
        supersede() or commit_new_version(). Approval-set binding lives on
        transition() for the same reason, so the version check can gate it.

        Requires the content to exist and its digest to match, so a lifecycle
        record can never point at content that is absent or different.
        """
        key = (plan_id, plan_version)
        if key not in self._content:
            raise PlanNotFoundError(plan_id, plan_version, lookup_kind="plan_content")
        stored = self._content[key]
        if stored["plan_digest"] != plan_digest:
            raise DigestMismatchError(
                plan_id=plan_id,
                plan_version=plan_version,
                expected_plan_digest=plan_digest,
                recomputed_plan_digest=stored["plan_digest"],
            )
        if key in self._lifecycle:
            # A lifecycle record is mutable and may already have progressed past
            # DRAFT. Re-creating it would silently rewind status and the
            # approval-set binding, breaking
            # security_controls.approvals_bound_to_plan_version_and_digest.
            raise LifecycleAlreadyExistsError(
                plan_id, plan_version, self._lifecycle[key]["status"]
            )

        record = {
            "plan_id": plan_id,
            "plan_version": plan_version,
            "plan_digest": plan_digest,
            "status": CREATION_STATUS,
            "approval_set_id": None,
            "published_version": None,
            "updated_at": ts,
        }
        # Build the candidate, validate it, THEN write. Never write-then-check:
        # a failed check would leave a malformed record in the store of record.
        #
        # This catches what a hardcoded status cannot (audit finding P0-1): `ts`
        # was stored verbatim into `updated_at` with no format check, so
        # `ts='not a timestamp at all'`, `ts=''`, `ts=None` and `ts=12345` were
        # all accepted even though $defs.plan_lifecycle requires a date-time
        # string.
        self._validate_record(record, "plan_lifecycle", plan_id)

        self._lifecycle[key] = record
        return copy.deepcopy(record)

    # ------------------------------------------------------------------- read

    def get_content(self, plan_id: str, plan_version: int | None = None) -> dict:
        """Immutable plan content. `plan_version=None` means the latest."""
        version = self._resolve_version(plan_id, plan_version)
        return copy.deepcopy(self._content[(plan_id, version)])

    def get_lifecycle(self, plan_id: str, plan_version: int | None = None) -> dict:
        """Mutable lifecycle record. `plan_version=None` means the latest."""
        version = self._resolve_version(plan_id, plan_version)
        key = (plan_id, version)
        if key not in self._lifecycle:
            raise PlanNotFoundError(plan_id, version, lookup_kind="plan_lifecycle")
        return copy.deepcopy(self._lifecycle[key])

    def has(self, plan_id: str, plan_version: int | None = None) -> bool:
        try:
            version = self._resolve_version(plan_id, plan_version)
        except PlanNotFoundError:
            return False
        return (plan_id, version) in self._content

    def latest_version(self, plan_id: str) -> int:
        """Highest stored version for a plan_id."""
        return self._resolve_version(plan_id, None)

    def versions(self, plan_id: str) -> list[int]:
        """All stored versions, ascending."""
        return sorted(v for (pid, v) in self._content if pid == plan_id)

    def verify_digest(self, plan_id: str, plan_version: int | None = None) -> str:
        """Recompute the digest from stored bytes.

        plan_store.retention: "digests are recomputable from stored content at any
        time". This is that promise, executable.
        """
        content = self.get_content(plan_id, plan_version)
        recomputed = canonical_plan_digest(content)
        if recomputed != content["plan_digest"]:
            raise DigestMismatchError(
                plan_id=content["plan_id"],
                plan_version=content["plan_version"],
                expected_plan_digest=content["plan_digest"],
                recomputed_plan_digest=recomputed,
            )
        return recomputed

    # -------------------------------------------------------------- mutate

    def transition(
        self,
        plan_id: str,
        plan_version: int,
        new_status: str,
        ts: str,
        approval_set_id: str | None = None,
        published_version: int | None = None,
        expected_plan_version: int | None = None,
    ) -> dict:
        """Apply a public lifecycle transition.

        SUPERSEDED is not a public transition target. It is the atomic result of
        supersede()/commit_new_version(), which also records the required durable
        invalidation event. Letting callers reach it here would create a lifecycle
        that dump_state() can write but load_state() must reject for missing audit
        history.
        """
        return self._transition(
            plan_id,
            plan_version,
            new_status,
            ts,
            approval_set_id=approval_set_id,
            published_version=published_version,
            expected_plan_version=expected_plan_version,
            allow_superseded=False,
        )

    def _transition(
        self,
        plan_id: str,
        plan_version: int,
        new_status: str,
        ts: str,
        approval_set_id: str | None = None,
        published_version: int | None = None,
        expected_plan_version: int | None = None,
        *,
        allow_superseded: bool,
    ) -> dict:
        """Change lifecycle status.

        `plan_version` is REQUIRED and must be an int — never None. Audit finding
        F11: with `plan_version=None` this resolved to `max(versions)`, so
        `transition(pid, None, "PUBLISHED")` would publish whatever version
        happened to be newest, which may be a regenerated-but-unapproved one.
        Publishing must name the exact version an approval set is bound to
        (security_controls.approvals_bound_to_plan_version_and_digest). Reads may
        still default to latest; a state MUTATION may not.

        `expected_plan_version` is optimistic concurrency control, and audit
        finding P0-2 is about what it was compared against. The first version
        compared it with the caller's own `plan_version` argument — two values
        that both came from the caller, so the check was a tautology that only
        ever caught a caller contradicting itself. It is now compared with
        `current_active_version(plan_id)`, the store's own notion of which
        version is live. That is what actually stops a stale approval set from
        publishing a regenerated plan
        (security_controls.approvals_bound_to_plan_version_and_digest).

        A status that opens or grants authority (AWAITING_APPROVAL, APPROVED,
        PUBLISHED) additionally requires the target version to be active. Without
        this, `put_content(v2)`
        followed by `transition(v1, "PUBLISHED")` publishes a superseded-in-fact
        plan even when no one passed `expected_plan_version`.
        `commit_new_version()` closes the same hole structurally by marking the
        old version SUPERSEDED; this check closes it for callers that write
        versions by hand.

        The full workflow graph is not re-implemented here —
        workflow.transitions owns it. The store enforces only lifecycle-local
        impossibilities that must never be persisted under any workflow.
        """
        if not isinstance(plan_version, int) or isinstance(plan_version, bool):
            raise InvalidContentError(
                str(plan_id), plan_version if isinstance(plan_version, int) else -1,
                json_path="plan_version",
                reason=f"a state mutation must name an explicit int version, got "
                       f"{plan_version!r}; None would publish the latest version, "
                       f"which may be unapproved",
            )
        version = self._resolve_version(plan_id, plan_version)

        key = (plan_id, version)
        if key not in self._lifecycle:
            raise PlanNotFoundError(plan_id, version, lookup_kind="plan_lifecycle")
        if new_status not in LIFECYCLE_STATUSES:
            raise ValueError(
                f"status {new_status!r} is not in $defs.plan_lifecycle.properties.status.enum"
            )

        record = self._lifecycle[key]
        if new_status == "SUPERSEDED" and not allow_superseded:
            raise TransitionNotAllowedError(
                plan_id, version, record["status"], new_status
            )
        if record["status"] in _TERMINAL:
            # SUPERSEDED is terminal: its approval sets are invalidated and it can
            # never be published, so nothing may move it again.
            #
            # Checked BEFORE the two version gates below, and the ordering is
            # load-bearing rather than cosmetic. Both gates report "you are acting
            # on the wrong version", which advises the caller to retry against the
            # active one. For a superseded version that advice is wrong — v1 is
            # dead permanently and no retry can succeed. Reporting the terminal
            # state instead tells the caller the truth.
            #
            # This makes supersede() deliberately NON-idempotent — a second call
            # raises instead of silently double-recording the event. That matters:
            # supersede history is unique per version, and a duplicated event
            # could cause the approval service to repeat work. Calling supersede
            # twice is a caller bug, and per
            # StoreInvariantError it surfaces as a crash rather than a tool_error.
            raise TransitionNotAllowedError(
                plan_id, version, record["status"], new_status
            )
        if record["status"] == "PUBLISHED" and new_status != "SUPERSEDED":
            # The workflow may re-enter planning from PUBLISHED. Regeneration
            # retires this immutable version through SUPERSEDED; it must not
            # rewrite the already-published lifecycle back to a mutable state.
            raise TransitionNotAllowedError(
                plan_id, version, record["status"], new_status
            )

        # P0-2: compare against the STORE's active version, not the caller's own
        # argument. The first version compared these two caller-supplied values
        # with each other, which was a tautology.
        active = self.current_active_version(plan_id)
        if expected_plan_version is not None and expected_plan_version != active:
            raise VersionConflictError(plan_id, expected_plan_version, active)

        if new_status in _ACTIVE_ONLY_STATUSES and version != active:
            # A stale version may not enter approval, be approved, or be published.
            # PLAN_VERSION_CONFLICT is the registered code for acting on the wrong
            # version.
            raise VersionConflictError(plan_id, version, active)

        # Build the candidate, validate it, THEN replace. The first version
        # mutated `record` in place, so a rejected write could leave the stored
        # record half-changed (status updated, updated_at rejected). Copying
        # first makes the mutation all-or-nothing, and validating the candidate
        # against $defs.plan_lifecycle is what rejects a junk `ts` — audit
        # finding P0-1, which the enum check alone could not see.
        if approval_set_id is not None and version != active:
            raise VersionConflictError(plan_id, version, active)

        candidate = dict(record)
        candidate["status"] = new_status
        candidate["updated_at"] = ts
        if approval_set_id is not None:
            if record["approval_set_id"] not in (None, approval_set_id):
                raise TransitionNotAllowedError(
                    plan_id, version, record["status"], new_status
                )
            candidate["approval_set_id"] = approval_set_id
        if published_version is not None:
            candidate["published_version"] = published_version

        # Local lifecycle invariants.  These do not duplicate the workflow state
        # machine: they only prevent records that can never be true regardless of
        # orchestration (an approval state without a set, or publication without
        # an approved predecessor and an exact version binding).
        if new_status == "AWAITING_APPROVAL":
            if candidate["approval_set_id"] is None:
                raise TransitionNotAllowedError(
                    plan_id, version, record["status"], new_status
                )
        elif new_status == "APPROVED":
            if candidate["approval_set_id"] is None:
                raise TransitionNotAllowedError(
                    plan_id, version, record["status"], new_status
                )
        elif new_status == "PUBLISHED":
            if (
                record["status"] != "APPROVED"
                or candidate["approval_set_id"] is None
                or candidate["published_version"] != version
            ):
                raise TransitionNotAllowedError(
                    plan_id, version, record["status"], new_status
                )
        elif published_version is not None:
            raise TransitionNotAllowedError(
                plan_id, version, record["status"], new_status
            )
        self._validate_record(candidate, "plan_lifecycle", plan_id)

        self._lifecycle[key] = candidate
        return copy.deepcopy(candidate)

    def supersede(self, plan_id: str, plan_version: int, ts: str) -> dict:
        """Mark a version SUPERSEDED and record that its approvals need invalidating.

        plan_store.versioning: "a superseded version moves to
        lifecycle.status=SUPERSEDED and its approval sets are invalidated."

        This module does not own approval sets, so it RECORDS the fact in
        append-only `superseded_events`; the approval service consumes the durable
        outbox via pending_superseded()/acknowledge_superseded(). It never reaches
        into another module's records.

        The event carries `invalidation_cause` from the closed enum in
        $defs.error_details_approval_set_invalidated, because
        workflow.approval_invalidation requires invalidation to be atomic and
        approval_set_lifecycle.invalidation distinguishes five causes. An event
        without one would leave the approval service guessing which applied.

        SUPERSEDED is not an authority-granting status, so this works on a stale
        version by design — that is precisely what commit_new_version() needs.
        """
        record = self._transition(
            plan_id,
            plan_version,
            "SUPERSEDED",
            ts,
            allow_superseded=True,
        )
        event = {
            "plan_id": plan_id,
            "plan_version": plan_version,
            "plan_digest": record["plan_digest"],
            "approval_set_id": record["approval_set_id"],
            "superseded_at": ts,
            "invalidation_cause": _CAUSE_SUPERSEDED,
        }
        self._superseded.append(event)
        return record

    def commit_new_version(self, content: dict, ts: str) -> dict:
        """Atomically add a new plan version and retire the one it replaces.

        One call performs, in order:
          1. validate the content against $defs.plan_content (via put_content)
          2. check version continuity — the new version must be latest + 1
          3. write the content
          4. create its lifecycle record (DRAFT)
          5. supersede the previously active version
          6. record the approval-invalidation event

        Any step that fails leaves the store EXACTLY as it was. That is the point
        of this method and the reason put_content() does not do it: a caller that
        wrote content, then found the supersede failed, would be left with two
        live versions and an approval set bound to a plan that is no longer
        current — the precise inconsistency plan_store.versioning forbids.

        The old version is only superseded when it has a lifecycle record. If it
        has none there is no status to move and no approval binding to
        invalidate; _ACTIVE_ONLY_STATUSES still stops it entering approval or
        published later, since it is no longer the active version.

        Returns the new version's lifecycle record.
        """
        if not isinstance(content, dict):
            raise TypeError(f"content must be a dict, got {type(content).__name__}")

        plan_id = content.get("plan_id")
        plan_version = content.get("plan_version")
        if (
            not isinstance(plan_id, str)
            or not isinstance(plan_version, int)
            or isinstance(plan_version, bool)
        ):
            raise InvalidContentError(
                str(plan_id), plan_version if isinstance(plan_version, int) else -1,
                json_path="plan_id" if not isinstance(plan_id, str) else "plan_version",
                reason=f"plan_id/plan_version must be str/non-bool int, got "
                       f"{type(plan_id).__name__}/{type(plan_version).__name__}",
            )

        # Snapshot for rollback. An in-memory store, so a deep copy is both the
        # simplest and the most obviously-correct transaction here.
        saved_content = copy.deepcopy(self._content)
        saved_lifecycle = copy.deepcopy(self._lifecycle)
        saved_events = list(self._superseded)
        saved_acks = copy.deepcopy(self._superseded_acks)

        try:
            # Step 1 + 3: the write path validates the schema and both digest
            # identities before storing, so steps 2 and 4 run against content
            # that is already known legal.
            known = [v for (pid, v) in self._content if pid == plan_id]
            previous = max(known) if known else None
            if previous is not None and plan_version != previous + 1:
                # plan_store.versioning: "plan_version increments on every
                # regeneration". Skipping a number would leave a gap no reader can
                # explain, and reusing one would collide with write-once content.
                raise VersionConflictError(plan_id, previous + 1, plan_version)

            # allow_new_version is what lets THIS method add a version at all:
            # put_content() refuses to (P1-b), because only here are continuity,
            # lifecycle, supersede and the invalidation event performed together.
            # Harmless for a first version, where `previous is None`.
            digest = self._store_content(content, allow_new_version=True)

            # Step 4: lifecycle for the new version, DRAFT until approved.
            record = self.create_lifecycle(plan_id, plan_version, digest, ts=ts)

            # Steps 5 + 6: retire the version this one replaces.
            if previous is not None and (plan_id, previous) in self._lifecycle:
                self.supersede(plan_id, previous, ts)

            return record
        except Exception:
            self._content = saved_content
            self._lifecycle = saved_lifecycle
            self._superseded = saved_events
            self._superseded_acks = saved_acks
            raise

    def pending_superseded(self) -> list[dict]:
        """Return unacknowledged supersede events without deleting audit history.

        Delivery is intentionally at-least-once.  The approval service must make
        invalidation idempotent and call acknowledge_superseded() only after its
        own transaction commits.  That closes the crash window created by the
        former destructive drain, which could lose an event before invalidation.
        """
        return [
            copy.deepcopy(event)
            for event in self._superseded
            if (event["plan_id"], event["plan_version"]) not in self._superseded_acks
        ]

    def acknowledge_superseded(
        self,
        plan_id: str,
        plan_version: int,
        plan_digest: str,
        approval_set_id: str | None,
        ts: str,
    ) -> dict:
        """Acknowledge one invalidation after the consumer has committed it.

        Repeating the same acknowledgement is idempotent.  The event remains in
        append-only history; only its separate delivery record is added.
        """
        key = (plan_id, plan_version)
        matching = [
            event for event in self._superseded
            if (event["plan_id"], event["plan_version"]) == key
        ]
        if len(matching) != 1:
            raise StoreInvariantError(
                f"cannot acknowledge supersede event {key}: expected one history "
                f"record, found {len(matching)}"
            )
        event = matching[0]
        if (
            event["plan_digest"] != plan_digest
            or event["approval_set_id"] != approval_set_id
        ):
            raise StoreInvariantError(
                f"cannot acknowledge supersede event {key}: digest or approval-set "
                f"binding does not match the immutable history record"
            )
        existing = self._superseded_acks.get(key)
        if existing is not None:
            return copy.deepcopy(existing)
        ack = {
            "plan_id": plan_id,
            "plan_version": plan_version,
            "plan_digest": plan_digest,
            "approval_set_id": approval_set_id,
            "acknowledged_at": ts,
        }
        try:
            validate_shape_against_schema(ack, SUPERSEDED_ACK_SCHEMA, "superseded_ack")
        except SchemaValidationError as exc:
            raise SchemaViolationError(exc) from exc
        if datetime.fromisoformat(ts.replace("Z", "+00:00")) < datetime.fromisoformat(
            event["superseded_at"].replace("Z", "+00:00")
        ):
            raise StoreInvariantError(
                f"cannot acknowledge supersede event {key} before it was emitted"
            )
        self._superseded_acks[key] = ack
        return copy.deepcopy(ack)

    # ----------------------------------------------------------- persistence

    def dump_state(self, path: str | Path) -> str:
        """Write canonical JSON so the evidence pack is byte-reproducible.

        LF newlines, sorted keys, no whitespace — the same canonical form the
        digest uses, and the same rule .gitattributes enforces on the contract.

        `superseded_events` is sorted here rather than emitted in insertion
        order, and that was a real defect (audit finding P1-b, found while
        rewriting the insertion-order test). content and lifecycle are keyed by
        (plan_id, plan_version) and were already sorted; the events were
        `list(self._superseded)`, so two stores holding the same plans in a
        different insertion order dumped different bytes. A single-plan test
        cannot see this — with one plan there is exactly one event — which is why
        it survived until the test covered two plans. The evidence pack hashes
        these bytes, so an order-dependent dump is an order-dependent hash.

        Sort key mirrors the F12 discipline: total, and type-ranked so a null
        approval_set_id cannot raise TypeError when compared with a string.
        """
        state = {
            "content": [
                self._content[k] for k in sorted(self._content)
            ],
            "lifecycle": [
                self._lifecycle[k] for k in sorted(self._lifecycle)
            ],
            "superseded_events": sorted(self._superseded, key=_event_sort_key),
            "superseded_acks": sorted(self._superseded_acks.values(), key=_ack_sort_key),
        }
        text = canonical_json(state)
        Path(path).write_bytes((text + "\n").encode("utf-8"))
        return text

    def load_state(self, path: str | Path) -> None:
        """Restore a dumped state, validating every record AND every relationship.

        A dump whose content no longer hashes to its declared digest is refused —
        loading it would resurrect a corrupted plan into the store of record.

        Four audits hit this method, each finding a different layer missing:

        F1 — lifecycle records were restored with NO checks, so a hand-edited
        dump could inject a status outside the enum, or a plan_digest disagreeing
        with the content it binds to, resurrecting a plan as PUBLISHED/APPROVED
        when its content was never approved.

        P1 — the restore used dict comprehensions, so a dump carrying two records
        with the same `(plan_id, plan_version)` silently kept only the last. That
        is not a load, it is a swap: the surviving record is whichever appeared
        later in the file. `superseded_events` had no validation at all, so a
        fabricated event for a plan that never existed would be handed to the
        approval service, which would invalidate a real approval set on the
        strength of it.

        A4 — a successfully consumed destructive event queue could no longer
        round-trip, forged causes/timestamps and version gaps were accepted, and
        acknowledgements had no durable representation. Event history is now
        append-only and acknowledgements are validated as separate records.

        The fix is layered, because the two layers catch different things:
          1. SHAPE — the whole envelope against STATE_SCHEMA (closed top level,
             date-time formats, digest patterns, closed enums), plus each content
             record against the contract's own $defs.plan_content
          2. RELATIONSHIP — duplicate keys, version continuity, digests that
             disagree between records, lifecycle authority, event identity, and
             acknowledgement binding. No JSON Schema can
             express "element A's field must equal element B's field where the
             tuple keys match", so this stays explicit code.

        Nothing is written until every check passes, so a bad dump cannot
        half-load and leave the store inconsistent.
        """
        raw = Path(path).read_bytes().decode("utf-8")
        state = json.loads(raw)

        # ---- layer 1: shape ------------------------------------------------
        # Validated through the production entry point, so this is the same
        # compiler the tests use — not a test-only validator. Wrapped so the
        # store raises ONE exception root (SchemaViolationError), keeping the
        # tool layer's error mapping uniform.
        try:
            validate_shape_against_schema(state, STATE_SCHEMA, "plan_store_state")
        except SchemaValidationError as exc:
            raise SchemaViolationError(exc) from exc

        content_records = state["content"]
        lifecycle_records = state["lifecycle"]
        events = state["superseded_events"]
        acknowledgements = state.get("superseded_acks", [])

        # Every content record must satisfy the contract, and both digest
        # identities must hold. assert_digest_consistent covers the identities;
        # _validate_record covers legality. Digest consistency alone is not
        # enough — see put_content's gate 2 (audit finding P0-1).
        content_by_key: dict[tuple[str, int], dict] = {}
        for record in content_records:
            self._validate_record(record, "plan_content", record.get("plan_id"))
            assert_digest_consistent(record)
            key = (record["plan_id"], record["plan_version"])
            if key in content_by_key:
                # A duplicate is a swap, not a load. Refuse it rather than let
                # the last one win silently.
                raise InvalidContentError(
                    str(record["plan_id"]), record["plan_version"],
                    json_path="content",
                    reason=f"duplicate (plan_id, plan_version) {key} in the dump; "
                           f"loading it would silently keep only the last record",
                )
            content_by_key[key] = record

        # ---- layer 2: relationships ----------------------------------------
        lifecycle_by_key: dict[tuple[str, int], dict] = {}
        for rec in lifecycle_records:
            key = (rec["plan_id"], rec["plan_version"])
            if key in lifecycle_by_key:
                raise InvalidContentError(
                    str(rec["plan_id"]), rec["plan_version"],
                    json_path="lifecycle",
                    entity_type="plan_lifecycle",
                    reason=f"duplicate (plan_id, plan_version) {key} in the dump; "
                           f"loading it would silently keep only the last record",
                )
            bound = content_by_key.get(key)
            if bound is None:
                # A lifecycle record pointing at absent content is exactly the
                # inconsistency create_lifecycle forbids on the live path.
                raise PlanNotFoundError(
                    str(rec["plan_id"]), rec["plan_version"],
                    lookup_kind="plan_content",
                )
            if rec["plan_digest"] != bound["plan_digest"]:
                raise DigestMismatchError(
                    plan_id=str(rec["plan_id"]),
                    plan_version=rec["plan_version"],
                    expected_plan_digest=rec["plan_digest"],
                    recomputed_plan_digest=bound["plan_digest"],
                )
            status = rec["status"]
            if status in _ACTIVE_ONLY_STATUSES and rec["approval_set_id"] is None:
                raise InvalidContentError(
                    str(rec["plan_id"]), rec["plan_version"],
                    json_path="lifecycle", entity_type="plan_lifecycle",
                    reason=f"{status} requires a bound approval_set_id",
                )
            if status == "PUBLISHED" and rec["published_version"] != rec["plan_version"]:
                raise InvalidContentError(
                    str(rec["plan_id"]), rec["plan_version"],
                    json_path="lifecycle", entity_type="plan_lifecycle",
                    reason=f"PUBLISHED requires published_version to equal plan_version "
                           f"{rec['plan_version']}, got {rec['published_version']!r}",
                )
            if status not in {"PUBLISHED", "SUPERSEDED"} and rec["published_version"] is not None:
                raise InvalidContentError(
                    str(rec["plan_id"]), rec["plan_version"],
                    json_path="lifecycle", entity_type="plan_lifecycle",
                    reason=f"{status} cannot carry published_version "
                           f"{rec['published_version']!r}",
                )
            lifecycle_by_key[key] = rec

        # Supersede events must describe something in this dump. An event for an
        # unknown plan is a fabricated invalidation, and the approval service
        # would act on it.
        #
        # Audit finding P1-a: checking the event against CONTENT is not enough.
        # The event's whole purpose is to tell the approval service which
        # approval set to invalidate, so it has to agree with the lifecycle
        # record that actually held that binding. Without these checks a
        # hand-edited dump could rebind an event to a different approval set,
        # duplicate an event so one set is invalidated twice while another is
        # never touched, or flip a superseded lifecycle back to APPROVED and keep
        # the event — resurrecting authority the event claims was withdrawn.
        event_count: dict[tuple[str, int], int] = {}
        for event in events:
            from datetime import datetime
            try:
                stamp = event.get("superseded_at")
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                raise SchemaViolationError(SchemaValidationError([], "superseded_event", str(event.get("plan_id"))))
            key = (event["plan_id"], event["plan_version"])
            if key not in content_by_key:
                raise PlanNotFoundError(
                    str(event["plan_id"]), event["plan_version"],
                    lookup_kind="plan_content",
                )
            bound = content_by_key[key]
            if event["plan_digest"] != bound["plan_digest"]:
                raise DigestMismatchError(
                    plan_id=str(event["plan_id"]),
                    plan_version=event["plan_version"],
                    expected_plan_digest=event["plan_digest"],
                    recomputed_plan_digest=bound["plan_digest"],
                )

            # (1) the event must bind to a lifecycle record for the same version
            lc = lifecycle_by_key.get(key)
            if lc is None:
                raise InvalidContentError(
                    str(event["plan_id"]), event["plan_version"],
                    json_path="superseded_events",
                    entity_type="superseded_event",
                    reason="a supersede event has no lifecycle record for the same "
                           "(plan_id, plan_version); nothing was superseded",
                )
            # (2) and that record must actually be superseded
            if lc["status"] != "SUPERSEDED":
                raise InvalidContentError(
                    str(event["plan_id"]), event["plan_version"],
                    json_path="superseded_events",
                    entity_type="superseded_event",
                    reason=f"supersede event exists but the lifecycle record is "
                           f"{lc['status']!r}, not SUPERSEDED; the event would "
                           f"invalidate approvals for a version that was never retired",
                )
            # (3) digest and approval-set binding must agree exactly
            if event["approval_set_id"] != lc["approval_set_id"]:
                raise InvalidContentError(
                    str(event["plan_id"]), event["plan_version"],
                    json_path="superseded_events",
                    entity_type="superseded_event",
                    reason=f"supersede event names approval_set_id "
                           f"{event['approval_set_id']!r} but the lifecycle record "
                           f"binds {lc['approval_set_id']!r}; the approval service "
                           f"would invalidate the wrong set",
                )
            if event["invalidation_cause"] != _CAUSE_SUPERSEDED:
                raise InvalidContentError(
                    str(event["plan_id"]), event["plan_version"],
                    json_path="superseded_events",
                    entity_type="superseded_event",
                    reason=f"supersede event has invalidation_cause "
                           f"{event['invalidation_cause']!r}; this outbox only emits "
                           f"{_CAUSE_SUPERSEDED!r}",
                )
            if event["superseded_at"] != lc["updated_at"]:
                raise InvalidContentError(
                    str(event["plan_id"]), event["plan_version"],
                    json_path="superseded_events",
                    entity_type="superseded_event",
                    reason=f"superseded_at {event['superseded_at']!r} does not match "
                           f"the terminal lifecycle updated_at {lc['updated_at']!r}",
                )
            # (4) at most one immutable history event per version. Delivery may
            # retry, but the event itself must never be duplicated.
            event_count[key] = event_count.get(key, 0) + 1
            if event_count[key] > 1:
                raise InvalidContentError(
                    str(event["plan_id"]), event["plan_version"],
                    json_path="superseded_events",
                    entity_type="superseded_event",
                    reason=f"{event_count[key]} supersede events for the same "
                           f"(plan_id, plan_version); a second history event is "
                           f"fabricated",
                )

        # (5) the converse: every superseded lifecycle has exactly one immutable
        # history event, even when it had no approval set.  Delivery acknowledgement
        # is separate, so consuming the message never erases the audit trail.
        for key, lc in lifecycle_by_key.items():
            if lc["status"] == "SUPERSEDED":
                if event_count.get(key, 0) != 1:
                    raise InvalidContentError(
                        str(key[0]), key[1],
                        json_path="lifecycle",
                        entity_type="plan_lifecycle",
                        reason=f"lifecycle is SUPERSEDED with approval_set_id "
                               f"{lc['approval_set_id']!r} but has "
                               f"{event_count.get(key, 0)} supersede events; the "
                               f"supersede audit history is incomplete",
                    )

        ack_by_key: dict[tuple[str, int], dict] = {}
        event_by_key = {
            (event["plan_id"], event["plan_version"]): event for event in events
        }
        for ack in acknowledgements:
            key = (ack["plan_id"], ack["plan_version"])
            if key in ack_by_key:
                raise InvalidContentError(
                    str(ack["plan_id"]), ack["plan_version"],
                    json_path="superseded_acks", entity_type="superseded_ack",
                    reason=f"duplicate acknowledgement for {key}",
                )
            event = event_by_key.get(key)
            if event is None:
                raise InvalidContentError(
                    str(ack["plan_id"]), ack["plan_version"],
                    json_path="superseded_acks", entity_type="superseded_ack",
                    reason="acknowledgement has no matching supersede history event",
                )
            if (
                ack["plan_digest"] != event["plan_digest"]
                or ack["approval_set_id"] != event["approval_set_id"]
            ):
                raise InvalidContentError(
                    str(ack["plan_id"]), ack["plan_version"],
                    json_path="superseded_acks", entity_type="superseded_ack",
                    reason="acknowledgement does not match the event digest and approval binding",
                )
            if datetime.fromisoformat(
                ack["acknowledged_at"].replace("Z", "+00:00")
            ) < datetime.fromisoformat(event["superseded_at"].replace("Z", "+00:00")):
                raise InvalidContentError(
                    str(ack["plan_id"]), ack["plan_version"],
                    json_path="superseded_acks", entity_type="superseded_ack",
                    reason="acknowledgement predates the supersede event",
                )
            ack_by_key[key] = ack

        # ---- layer 3: version authority ------------------------------------
        # Audit finding P1-b, second half. put_content() now refuses to add a
        # version to a plan that already has one, so the live API can no longer
        # produce a stale version that still holds APPROVED or PUBLISHED —
        # commit_new_version() supersedes the previous one as it adds the next.
        #
        # A dump is the other way in, and it was wide open: appending a v2
        # content record to a dump whose v1 is APPROVED loaded cleanly, giving
        # current_active_version() == 2 with v1 still APPROVED and its approval
        # set still live. Measured, not assumed — that is the state this layer
        # refuses.
        #
        # The rule reuses _ACTIVE_ONLY_STATUSES rather than requiring SUPERSEDED,
        # and that distinction was also measured. Requiring SUPERSEDED refused
        # non-authority states the live API legitimately produces — a stale
        # version left in DRAFT, PROPOSED or BLOCKED — so dump/load lost
        # round-trip fidelity. AWAITING_APPROVAL was included in that historical
        # list until the fourth audit proved it can create a fresh approval
        # binding for retired content; it now belongs to _ACTIVE_ONLY_STATUSES.
        # Reusing that one set here keeps load and transition from drifting.
        versions_by_plan: dict[str, list[int]] = {}
        for pid, ver in content_by_key:
            versions_by_plan.setdefault(pid, []).append(ver)
        for pid, versions in versions_by_plan.items():
            versions = sorted(versions)
            for previous, current in zip(versions, versions[1:]):
                if current != previous + 1:
                    raise InvalidContentError(
                        pid, current, json_path="content", entity_type="plan_content",
                        reason=f"non-contiguous versions {previous} -> {current}; every "
                               f"regeneration must increment plan_version by one",
                    )
            active = versions[-1]
            for ver in versions:
                if ver == active:
                    continue
                lc = lifecycle_by_key.get((pid, ver))
                if lc is not None and (
                    lc["status"] in _ACTIVE_ONLY_STATUSES
                    or (lc["approval_set_id"] is not None and lc["status"] != "SUPERSEDED")
                ):
                    raise InvalidContentError(
                        pid, ver,
                        json_path="lifecycle",
                        entity_type="plan_lifecycle",
                        reason=f"v{ver} is stale (active version is v{active}) but "
                               f"its lifecycle status is {lc['status']!r}; a stale "
                               f"version holding authority is what "
                               f"commit_new_version() exists to prevent",
                    )

        # ---- commit: every check passed ------------------------------------
        self._content = {k: copy.deepcopy(v) for k, v in content_by_key.items()}
        self._lifecycle = {k: copy.deepcopy(v) for k, v in lifecycle_by_key.items()}
        self._superseded = [copy.deepcopy(e) for e in events]
        self._superseded_acks = {k: copy.deepcopy(v) for k, v in ack_by_key.items()}

    # ---------------------------------------------------------------- internal

    @staticmethod
    def _validate_record(record: Any, def_name: str, entity_id: str | None) -> None:
        """Validate one record against `$defs/<def_name>` at a write boundary.

        Wraps `planpilot.validation.SchemaValidationError` into the store's own
        `SchemaViolationError` so every failure this store raises stays a
        StoreError with a registered code and schema-shaped details. One
        exception root is what lets the tool layer map errors without
        special-casing the validation package.

        This is audit finding P0-1. Before it existed no store write path
        validated against the contract at all.
        """
        if def_name == "plan_lifecycle":
            from datetime import datetime
            ts = record.get("updated_at") if isinstance(record, dict) else None
            try:
                if not isinstance(ts, str) or datetime.fromisoformat(ts.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                raise SchemaViolationError(SchemaValidationError([], "plan_lifecycle", entity_id))
        try:
            validate_against_contract(record, def_name, def_name, entity_id)
        except SchemaValidationError as exc:
            raise SchemaViolationError(exc) from exc

    def current_active_version(self, plan_id: str) -> int:
        """The version a state change must be bound to (audit finding P0-2).

        `plan_store.versioning` says a version increments on every regeneration
        and the superseded one moves to SUPERSEDED, so the highest stored version
        is the active one and every lower version is stale. An approval or
        publication therefore has to name that version explicitly.

        This is what `expected_plan_version` is now compared against. The first
        version compared it against the caller's own `plan_version` argument —
        two values that both came from the caller, so the check was a tautology
        that only ever caught a caller contradicting itself. It could not do what
        its docstring claimed: stop a stale approval set from publishing a
        regenerated plan.

        Raises PlanNotFoundError if the plan is unknown, so this is safe to call
        without a separate existence check.
        """
        versions = [v for (pid, v) in self._content if pid == plan_id]
        if not versions:
            raise PlanNotFoundError(plan_id, None, lookup_kind="plan_content")
        return max(versions)

    def _resolve_version(self, plan_id: str, plan_version: int | None) -> int:
        if plan_version is not None:
            if (plan_id, plan_version) not in self._content:
                raise PlanNotFoundError(plan_id, plan_version, lookup_kind="plan_content")
            return plan_version
        versions = [v for (pid, v) in self._content if pid == plan_id]
        if not versions:
            raise PlanNotFoundError(plan_id, None, lookup_kind="plan_content")
        return max(versions)

    def __len__(self) -> int:
        return len(self._content)

    def __repr__(self) -> str:
        plans = sorted({pid for pid, _ in self._content})
        return f"<PlanStore versions={len(self._content)} plans={len(plans)}>"
