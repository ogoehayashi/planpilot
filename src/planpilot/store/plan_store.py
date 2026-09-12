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

* Digests are verified on WRITE, not on read. A corrupt or fabricated plan never
  enters the store. This is the defence against the known gateway failure mode
  (starter-kit proxy.py: malformed tool-call XML once made an agent fabricate
  tool results).

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

from .digest import assert_digest_consistent, canonical_json, canonical_plan_digest
from .errors import (
    DigestMismatchError,
    IdempotencyConflictError,
    LifecycleAlreadyExistsError,
    PlanNotFoundError,
    TransitionNotAllowedError,
    VersionConflictError,
)

__all__ = ["PlanStore", "LIFECYCLE_STATUSES"]

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

        Verifies BOTH digest identities before storing, so a corrupt or fabricated
        plan is rejected at the door rather than discovered at approval time.

        Idempotent for identical bytes: a retried write must not fail. Raising on
        an identical rewrite would make every retry path a bug.

        Returns the verified digest.
        """
        if not isinstance(content, dict):
            raise TypeError(f"content must be a dict, got {type(content).__name__}")

        plan_id = content.get("plan_id")
        plan_version = content.get("plan_version")
        if not isinstance(plan_id, str) or not isinstance(plan_version, int):
            raise PlanNotFoundError(
                plan_id=str(plan_id), plan_version=None, lookup_kind="plan_content_key"
            )

        # Verify before storing. Raises DigestMismatchError with contract-shaped
        # details if either identity fails.
        digest = assert_digest_consistent(content)

        key = (plan_id, plan_version)
        if key in self._content:
            existing = self._content[key]
            if canonical_json(existing) == canonical_json(content):
                return digest  # identical bytes: idempotent retry
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
        plan_version: int | None,
        new_status: str,
        ts: str,
        approval_set_id: str | None = None,
        published_version: int | None = None,
        expected_plan_version: int | None = None,
    ) -> dict:
        """Change lifecycle status.

        `expected_plan_version` is optimistic concurrency control: if supplied and
        it does not match the resolved version, PLAN_VERSION_CONFLICT is raised and
        nothing is written. This is what stops a stale approval set from publishing
        a regenerated plan (security_controls.approvals_bound_to_plan_version_and_digest).

        The transition GRAPH is not re-implemented here — workflow.transitions owns
        it. Only the status enum is checked.
        """
        version = self._resolve_version(plan_id, plan_version)
        if expected_plan_version is not None and expected_plan_version != version:
            raise VersionConflictError(plan_id, expected_plan_version, version)

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
            # This makes supersede() deliberately NON-idempotent — a second call
            # raises instead of silently double-recording the event. That matters:
            # drain_superseded() is an exactly-once hand-off to the approval
            # service, and a duplicated event would invalidate the same approval
            # set twice. Calling supersede twice is a caller bug, and per
            # StoreInvariantError it surfaces as a crash rather than a tool_error.
            raise TransitionNotAllowedError(
                plan_id, version, record["status"], new_status
            )

        record["status"] = new_status
        record["updated_at"] = ts
        if approval_set_id is not None:
            record["approval_set_id"] = approval_set_id
        if published_version is not None:
            record["published_version"] = published_version
        return copy.deepcopy(record)

    def supersede(self, plan_id: str, plan_version: int, ts: str) -> dict:
        """Mark a version SUPERSEDED and record that its approvals need invalidating.

        plan_store.versioning: "a superseded version moves to
        lifecycle.status=SUPERSEDED and its approval sets are invalidated."

        This module does not own approval sets, so it RECORDS the fact in
        `superseded_events` for the approval service to consume via
        drain_superseded(). It never reaches into another module's records.
        """
        record = self.transition(plan_id, plan_version, "SUPERSEDED", ts)
        event = {
            "plan_id": plan_id,
            "plan_version": plan_version,
            "plan_digest": record["plan_digest"],
            "approval_set_id": record["approval_set_id"],
            "superseded_at": ts,
        }
        self._superseded.append(event)
        return record

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
        """Restore a dumped state, re-verifying every digest.

        A dump whose content no longer hashes to its declared digest is refused —
        loading it would resurrect a corrupted plan into the store of record.
        """
        raw = Path(path).read_bytes().decode("utf-8")
        state = json.loads(raw)

        content_records = state.get("content", [])
        for record in content_records:
            # verify BEFORE inserting, so a bad dump cannot half-load
            assert_digest_consistent(record)

        self._content = {(r["plan_id"], r["plan_version"]): copy.deepcopy(r) for r in content_records}
        self._lifecycle = {
            (r["plan_id"], r["plan_version"]): copy.deepcopy(r)
            for r in state.get("lifecycle", [])
        }
        self._superseded = list(state.get("superseded_events", []))

    # ---------------------------------------------------------------- internal

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
