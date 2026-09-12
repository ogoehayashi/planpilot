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
  approval service consumes `drain_superseded()` and invalidates.

* Lifecycle status is validated against the enum, not against a transition
  graph. The authoritative graph is workflow.transitions (11 states); lifecycle
  status is a projection of it. Re-implementing that graph here would create a
  second source of truth that can drift.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .digest import assert_digest_consistent, canonical_json, canonical_plan_digest
from .errors import (
    DigestMismatchError,
    IdempotencyConflictError,
    InvalidContentError,
    LifecycleAlreadyExistsError,
    PlanNotFoundError,
    SchemaViolationError,
    TransitionNotAllowedError,
    VersionConflictError,
)
from .persistence_schema import STATE_SCHEMA
from ..validation import (
    SchemaValidationError,
    validate as validate_against_contract,
    validate_shape as validate_shape_against_schema,
)

__all__ = ["PlanStore", "LIFECYCLE_STATUSES", "INVALIDATION_CAUSES"]

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

# Terminal for the purposes of this store: nothing leaves SUPERSEDED, because a
# superseded version's approvals are invalidated and it can never be published.
_TERMINAL = frozenset({"SUPERSEDED"})

# Statuses that GRANT AUTHORITY, and therefore may only be set on the store's
# active version (audit finding P0-2).
#
# Deliberately narrow. AWAITING_APPROVAL and PROPOSED are excluded on purpose:
# they REQUEST authority rather than confer it, and blocking them would break the
# legitimate flow of preparing an older draft while a newer one exists. APPROVED
# and PUBLISHED are the two that let a plan drive production or satisfy a
# publication precondition, so those two are what must be bound to the version an
# approval set was actually derived from.
#
# `commit_new_version()` makes the superseded version SUPERSEDED, which _TERMINAL
# then blocks independently — this set is the belt for callers that write
# versions by hand instead of going through commit_new_version().
_AUTHORITY_STATUSES = frozenset({"APPROVED", "PUBLISHED"})

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
        self._superseded: list[dict] = []

    # ------------------------------------------------------------------ write

    def put_content(self, content: dict) -> str:
        """Store immutable plan content. Write-once per (plan_id, plan_version).

        Three independent gates, in this order:

        1. key pre-flight — plan_id/plan_version must be a str and a non-bool int
        2. **schema validation against `$defs.plan_content`** (audit finding P0-1)
        3. both digest identities must hold

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
        status: str = "DRAFT",
        approval_set_id: str | None = None,
        published_version: int | None = None,
    ) -> dict:
        """Create the mutable lifecycle record for an already-stored content version.

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
        if status not in LIFECYCLE_STATUSES:
            raise ValueError(
                f"status {status!r} is not in $defs.plan_lifecycle.properties.status.enum"
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
            "status": status,
            "approval_set_id": approval_set_id,
            "published_version": published_version,
            "updated_at": ts,
        }
        # Build the candidate, validate it, THEN write. Never write-then-check:
        # a failed check would leave a malformed record in the store of record.
        #
        # This catches what the enum check above cannot (audit finding P0-1):
        # `ts` was stored verbatim into `updated_at` with no format check, so
        # `ts='not a timestamp at all'`, `ts=''`, `ts=None` and `ts=12345` were
        # all accepted even though $defs.plan_lifecycle requires a date-time
        # string. `published_version` had the same hole.
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

        A status that GRANTS AUTHORITY (APPROVED, PUBLISHED) additionally requires
        the target version to be the active one. Without this, `put_content(v2)`
        followed by `transition(v1, "PUBLISHED")` publishes a superseded-in-fact
        plan even when no one passed `expected_plan_version`.
        `commit_new_version()` closes the same hole structurally by marking the
        old version SUPERSEDED; this check closes it for callers that write
        versions by hand.

        The transition GRAPH is not re-implemented here — workflow.transitions owns
        it. Only the status enum is checked.
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
            # drain_superseded() is an exactly-once hand-off to the approval
            # service, and a duplicated event would invalidate the same approval
            # set twice. Calling supersede twice is a caller bug, and per
            # StoreInvariantError it surfaces as a crash rather than a tool_error.
            raise TransitionNotAllowedError(
                plan_id, version, record["status"], new_status
            )

        # P0-2: compare against the STORE's active version, not the caller's own
        # argument. The first version compared these two caller-supplied values
        # with each other, which was a tautology.
        active = self.current_active_version(plan_id)
        if expected_plan_version is not None and expected_plan_version != active:
            raise VersionConflictError(plan_id, expected_plan_version, active)

        if new_status in _AUTHORITY_STATUSES and version != active:
            # A stale version may not be approved or published. PLAN_VERSION_CONFLICT
            # is the registered code for "you are acting on the wrong version".
            raise VersionConflictError(plan_id, version, active)

        # Build the candidate, validate it, THEN replace. The first version
        # mutated `record` in place, so a rejected write could leave the stored
        # record half-changed (status updated, updated_at rejected). Copying
        # first makes the mutation all-or-nothing, and validating the candidate
        # against $defs.plan_lifecycle is what rejects a junk `ts` — audit
        # finding P0-1, which the enum check alone could not see.
        candidate = dict(record)
        candidate["status"] = new_status
        candidate["updated_at"] = ts
        if approval_set_id is not None:
            candidate["approval_set_id"] = approval_set_id
        if published_version is not None:
            candidate["published_version"] = published_version
        self._validate_record(candidate, "plan_lifecycle", plan_id)

        self._lifecycle[key] = candidate
        return copy.deepcopy(candidate)

    def supersede(self, plan_id: str, plan_version: int, ts: str) -> dict:
        """Mark a version SUPERSEDED and record that its approvals need invalidating.

        plan_store.versioning: "a superseded version moves to
        lifecycle.status=SUPERSEDED and its approval sets are invalidated."

        This module does not own approval sets, so it RECORDS the fact in
        `superseded_events` for the approval service to consume via
        drain_superseded(). It never reaches into another module's records.

        The event carries `invalidation_cause` from the closed enum in
        $defs.error_details_approval_set_invalidated, because
        workflow.approval_invalidation requires invalidation to be atomic and
        approval_set_lifecycle.invalidation distinguishes five causes. An event
        without one would leave the approval service guessing which applied.

        SUPERSEDED is not an authority-granting status, so this works on a stale
        version by design — that is precisely what commit_new_version() needs.
        """
        record = self.transition(plan_id, plan_version, "SUPERSEDED", ts)
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
        invalidate; _AUTHORITY_STATUSES still stops it being approved or
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

        try:
            # Step 1 + 3: put_content validates the schema and both digest
            # identities before writing, so steps 2 and 4 run against content
            # that is already known legal.
            known = [v for (pid, v) in self._content if pid == plan_id]
            previous = max(known) if known else None
            if previous is not None and plan_version != previous + 1:
                # plan_store.versioning: "plan_version increments on every
                # regeneration". Skipping a number would leave a gap no reader can
                # explain, and reusing one would collide with write-once content.
                raise VersionConflictError(plan_id, previous + 1, plan_version)

            digest = self.put_content(content)

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
            raise

    def drain_superseded(self) -> list[dict]:
        """Pop all pending supersede events for the approval service to consume.

        Draining (rather than peeking) makes the hand-off exactly-once: two
        consumers cannot both invalidate the same set, and a missed event cannot
        leave approvals live against a superseded plan.
        """
        events = self._superseded
        self._superseded = []
        return events

    # ----------------------------------------------------------- persistence

    def dump_state(self, path: str | Path) -> str:
        """Write canonical JSON so the evidence pack is byte-reproducible.

        LF newlines, sorted keys, no whitespace — the same canonical form the
        digest uses, and the same rule .gitattributes enforces on the contract.
        """
        state = {
            "content": [
                self._content[k] for k in sorted(self._content)
            ],
            "lifecycle": [
                self._lifecycle[k] for k in sorted(self._lifecycle)
            ],
            "superseded_events": list(self._superseded),
        }
        text = canonical_json(state)
        Path(path).write_bytes((text + "\n").encode("utf-8"))
        return text

    def load_state(self, path: str | Path) -> None:
        """Restore a dumped state, validating every record AND every relationship.

        A dump whose content no longer hashes to its declared digest is refused —
        loading it would resurrect a corrupted plan into the store of record.

        Three audits hit this method, each finding a different layer missing:

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

        The fix is layered, because the two layers catch different things:
          1. SHAPE — the whole envelope against STATE_SCHEMA (closed top level,
             date-time formats, digest patterns, closed enums), plus each content
             record against the contract's own $defs.plan_content
          2. RELATIONSHIP — duplicate keys, digests that disagree between a
             lifecycle record and the content it binds to, and supersede events
             pointing at plans that are not in the dump. No JSON Schema can
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
            lifecycle_by_key[key] = rec

        # Supersede events must describe something in this dump. An event for an
        # unknown plan is a fabricated invalidation, and the approval service
        # would act on it.
        for event in events:
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

        # ---- commit: every check passed ------------------------------------
        self._content = {k: copy.deepcopy(v) for k, v in content_by_key.items()}
        self._lifecycle = {k: copy.deepcopy(v) for k, v in lifecycle_by_key.items()}
        self._superseded = [copy.deepcopy(e) for e in events]

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
