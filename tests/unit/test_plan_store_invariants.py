"""Task 3.2 — PlanStore invariants.

Contract source: plan_store.records (plan_content immutable/write-once,
plan_lifecycle mutable), plan_store.versioning (superseded -> SUPERSEDED and its
approval sets invalidated), security_controls
(approvals_bound_to_plan_version_and_digest).

Also pins the error-code mapping decided in design.md: a write-once violation is
IDEMPOTENCY_CONFLICT (same key, different content — the code's registered
meaning), NOT PLAN_VERSION_CONFLICT.
"""

from __future__ import annotations

import copy

import pytest

from planpilot.store import LIFECYCLE_STATUSES, PlanStore, canonical_plan_digest
from planpilot.store.errors import (
    DigestMismatchError,
    IdempotencyConflictError,
    InvalidContentError,
    LifecycleAlreadyExistsError,
    PlanNotFoundError,
    StoreError,
    StoreInvariantError,
    TransitionNotAllowedError,
    VersionConflictError,
)


@pytest.fixture
def content(fixtures):
    """plan_content whose digest is self-consistent."""
    draft = fixtures.make_content()
    return fixtures.make_content(digest=canonical_plan_digest(draft))


@pytest.fixture
def store(content):
    s = PlanStore()
    s.put_content(content)
    return s


class TestContractEnumMirrors:
    def test_lifecycle_statuses_equal_the_contract_enum(self, fixtures):
        """The mirrored tuple must not drift from $defs.plan_lifecycle."""
        enum = fixtures.contract()["$defs"]["plan_lifecycle"]["properties"]["status"]["enum"]
        assert list(LIFECYCLE_STATUSES) == enum

    def test_every_store_error_code_is_registered(self, fixtures):
        """No invented codes: every code a store exception can carry is in the enum."""
        from planpilot.store.errors import RETRYABILITY

        registry = fixtures.contract()["$defs"]["tool_error"]["properties"]["error_code"]["enum"]
        for code in RETRYABILITY:
            assert code in registry, f"{code} is not a registered error_code"

    def test_retryability_matches_the_registry(self, fixtures):
        from planpilot.store.errors import RETRYABILITY

        registry = fixtures.contract()["tool_execution_contract"]["retryability_registry"]
        for code, retryable in RETRYABILITY.items():
            assert registry[code] is retryable, f"{code} retryability drifted"

    def test_invariant_errors_are_not_store_errors(self):
        """Programming errors must not be renderable as tool_errors."""
        assert not issubclass(StoreInvariantError, StoreError)
        assert not issubclass(TransitionNotAllowedError, StoreError)
        assert not issubclass(LifecycleAlreadyExistsError, StoreError)
        for cls in (TransitionNotAllowedError, LifecycleAlreadyExistsError):
            assert not hasattr(cls, "code"), f"{cls.__name__} must not carry a contract code"


class TestWriteOnce:
    def test_identical_rewrite_is_idempotent(self, store, content):
        assert store.put_content(copy.deepcopy(content)) == content["plan_digest"]
        assert len(store) == 1

    def test_different_content_same_key_is_rejected(self, store, content, fixtures):
        other = copy.deepcopy(content)
        other["kpis"]["on_time_rate"] = 0.5
        # make it internally consistent so the ONLY failure is write-once
        d = canonical_plan_digest(other)
        other["plan_digest"] = d
        other["engine"]["canonical_plan_hash"] = d
        with pytest.raises(IdempotencyConflictError) as exc:
            store.put_content(other)
        fixtures.validate(exc.value.details, "error_details_idempotency_conflict")

    def test_write_once_uses_idempotency_conflict_not_version_conflict(self, store, content):
        """PLAN_VERSION_CONFLICT would be wrong: expected == actual here."""
        other = copy.deepcopy(content)
        other["assumptions"] = ["changed"]
        d = canonical_plan_digest(other)
        other["plan_digest"] = d
        other["engine"]["canonical_plan_hash"] = d
        with pytest.raises(IdempotencyConflictError):
            store.put_content(other)
        with pytest.raises(IdempotencyConflictError) as exc:
            store.put_content(other)
        assert not isinstance(exc.value, VersionConflictError)

    def test_idempotency_key_is_plan_id_colon_version(self, store, content):
        other = copy.deepcopy(content)
        other["assumptions"] = ["changed"]
        d = canonical_plan_digest(other)
        other["plan_digest"] = d
        other["engine"]["canonical_plan_hash"] = d
        with pytest.raises(IdempotencyConflictError) as exc:
            store.put_content(other)
        assert exc.value.details["idempotency_key"] == f"{content['plan_id']}:1"

    def test_a_new_version_of_the_same_plan_is_allowed(self, store, content):
        """versioning: plan_version increments on every regeneration."""
        v2 = copy.deepcopy(content)
        v2["plan_version"] = 2
        v2["kpis"]["on_time_rate"] = 0.6
        d = canonical_plan_digest(v2)
        v2["plan_digest"] = d
        v2["engine"]["canonical_plan_hash"] = d
        store.put_content(v2)
        assert store.versions(content["plan_id"]) == [1, 2]
        assert store.latest_version(content["plan_id"]) == 2


class TestDigestVerifiedOnWrite:
    def test_inconsistent_digest_is_rejected_before_storing(self, fixtures):
        corrupt = fixtures.make_content()  # digest "0"*64, not the real one
        s = PlanStore()
        with pytest.raises(DigestMismatchError):
            s.put_content(corrupt)
        assert len(s) == 0, "a corrupt plan must not enter the store"

    def test_only_plan_digest_wrong(self, content):
        broken = copy.deepcopy(content)
        broken["plan_digest"] = "c" * 64
        with pytest.raises(DigestMismatchError):
            PlanStore().put_content(broken)

    def test_only_canonical_plan_hash_wrong(self, content):
        broken = copy.deepcopy(content)
        broken["engine"]["canonical_plan_hash"] = "d" * 64
        with pytest.raises(DigestMismatchError):
            PlanStore().put_content(broken)

    def test_fabricated_kpi_cannot_enter_the_store(self, content):
        """The gateway failure mode: a fabricated tool result must not persist."""
        fabricated = copy.deepcopy(content)
        fabricated["kpis"]["on_time_rate"] = 1.0
        fabricated["kpis"]["late_orders"] = 0
        # attacker does not recompute the digest (they cannot, without the content)
        with pytest.raises(DigestMismatchError):
            PlanStore().put_content(fabricated)


class TestImmutability:
    def test_caller_mutation_after_put_does_not_change_the_store(self, store, content):
        content["kpis"]["on_time_rate"] = 0.0
        assert store.get_content(content["plan_id"], 1)["kpis"]["on_time_rate"] == 0.8

    def test_mutating_a_returned_copy_does_not_change_the_store(self, store, content):
        got = store.get_content(content["plan_id"], 1)
        got["kpis"]["on_time_rate"] = 0.0
        assert store.get_content(content["plan_id"], 1)["kpis"]["on_time_rate"] == 0.8

    def test_two_reads_are_distinct_objects(self, store, content):
        a = store.get_content(content["plan_id"], 1)
        b = store.get_content(content["plan_id"], 1)
        assert a == b and a is not b

    def test_unknown_field_is_rejected_by_the_schema(self, content, fixtures):
        """plan_content has additionalProperties: false."""
        polluted = copy.deepcopy(content)
        polluted["status"] = "PUBLISHED"
        assert not fixtures.is_valid(polluted, "plan_content")


class TestLifecycle:
    def test_create_requires_existing_content(self, content):
        with pytest.raises(PlanNotFoundError):
            PlanStore().create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts="2026-09-14T08:00:00+08:00")

    def test_create_rejects_a_digest_that_does_not_match_the_stored_content(self, store, content):
        with pytest.raises(DigestMismatchError):
            store.create_lifecycle(content["plan_id"], 1, "f" * 64, ts="2026-09-14T08:00:00+08:00")

    def test_create_rejects_an_unknown_status(self, store, content):
        with pytest.raises(ValueError):
            store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                                   ts="2026-09-14T08:00:00+08:00", status="PUBLISHING")

    def test_created_record_conforms_to_the_schema(self, store, content, fixtures):
        rec = store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                                     ts=fixtures.T0)
        fixtures.validate(rec, "plan_lifecycle")

    def test_recreating_a_lifecycle_is_refused(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        with pytest.raises(LifecycleAlreadyExistsError):
            store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T1)

    def test_recreating_cannot_rewind_a_progressed_record(self, store, content, fixtures):
        """The reason recreation is refused: it would rewind status and binding."""
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                         approval_set_id="AS-001")
        with pytest.raises(LifecycleAlreadyExistsError):
            store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T2)
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "APPROVED"
        assert store.get_lifecycle(content["plan_id"], 1)["approval_set_id"] == "AS-001"

    def test_transition_updates_status_and_timestamp(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        rec = store.transition(content["plan_id"], 1, "PROPOSED", ts=fixtures.T1)
        assert rec["status"] == "PROPOSED"
        assert rec["updated_at"] == fixtures.T1
        fixtures.validate(rec, "plan_lifecycle")

    def test_transition_rejects_an_unknown_status(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        with pytest.raises(ValueError):
            store.transition(content["plan_id"], 1, "PUBLISHING", ts=fixtures.T1)

    def test_lifecycle_mutation_does_not_change_the_content_digest(self, store, content, fixtures):
        before = store.verify_digest(content["plan_id"], 1)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        for status in ("PROPOSED", "AWAITING_APPROVAL", "APPROVED", "PUBLISHED"):
            store.transition(content["plan_id"], 1, status, ts=fixtures.T1)
        assert store.verify_digest(content["plan_id"], 1) == before


class TestOptimisticConcurrency:
    def test_expected_version_mismatch_raises_and_writes_nothing(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        with pytest.raises(VersionConflictError) as exc:
            store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                             expected_plan_version=99)
        fixtures.validate(exc.value.details, "error_details_plan_version_conflict")
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"

    def test_expected_version_match_succeeds(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        rec = store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                               expected_plan_version=1)
        assert rec["status"] == "APPROVED"

    def test_stale_approval_cannot_publish_a_regenerated_plan(self, store, content, fixtures):
        """security_controls.approvals_bound_to_plan_version_and_digest, exercised."""
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                         approval_set_id="AS-001", expected_plan_version=1)

        v2 = copy.deepcopy(content)
        v2["plan_version"] = 2
        v2["kpis"]["on_time_rate"] = 0.6
        d2 = canonical_plan_digest(v2)
        v2["plan_digest"] = d2
        v2["engine"]["canonical_plan_hash"] = d2
        store.put_content(v2)

        # a caller still holding version 1 cannot drive version 2
        with pytest.raises(VersionConflictError):
            store.transition(content["plan_id"], 2, "PUBLISHED", ts=fixtures.T2,
                             expected_plan_version=1)
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "APPROVED"


class TestSupersede:
    def test_supersede_sets_status_and_records_an_event(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.supersede(content["plan_id"], 1, ts=fixtures.T2)

        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "SUPERSEDED"
        events = store.drain_superseded()
        assert len(events) == 1
        assert events[0]["approval_set_id"] == "AS-001"
        assert events[0]["plan_digest"] == content["plan_digest"]
        assert events[0]["superseded_at"] == fixtures.T2

    def test_supersede_does_not_mutate_any_approval_set(self, store, content, fixtures):
        """versioning says approvals ARE invalidated — by the approval service.

        This module records the fact and nothing more. Reaching into another
        module's records is how the orphan-spec defect class happens.
        """
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.supersede(content["plan_id"], 1, ts=fixtures.T2)
        # the binding is still recorded; invalidation is the consumer's job
        assert store.get_lifecycle(content["plan_id"], 1)["approval_set_id"] == "AS-001"

    def test_drain_is_exactly_once(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.supersede(content["plan_id"], 1, ts=fixtures.T1)
        assert len(store.drain_superseded()) == 1
        assert store.drain_superseded() == []
        assert store.drain_superseded() == []

    def test_supersede_is_not_idempotent(self, store, content, fixtures):
        """A second call must raise, not double-record the event.

        drain_superseded() is an exactly-once hand-off; a duplicated event would
        invalidate the same approval set twice.
        """
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.supersede(content["plan_id"], 1, ts=fixtures.T1)
        store.drain_superseded()
        with pytest.raises(TransitionNotAllowedError):
            store.supersede(content["plan_id"], 1, ts=fixtures.T2)
        assert store.drain_superseded() == [], "no second event may be recorded"

    def test_terminal_status_blocks_any_further_transition(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.supersede(content["plan_id"], 1, ts=fixtures.T1)
        for status in ("DRAFT", "PROPOSED", "APPROVED", "PUBLISHED", "SUPERSEDED"):
            with pytest.raises(TransitionNotAllowedError):
                store.transition(content["plan_id"], 1, status, ts=fixtures.T2)


class TestClockIsInjected:
    def test_no_datetime_now_in_store_sources(self):
        """A store that reads the clock cannot be tested deterministically."""
        import re
        from pathlib import Path

        store_dir = Path(__file__).resolve().parents[2] / "src" / "planpilot" / "store"
        offenders = []
        for py in store_dir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            # strip docstrings and comments before scanning, so this file's own
            # prose about the rule does not trip it
            stripped = re.sub(r'"""[\s\S]*?"""', "", text)
            stripped = re.sub(r"#.*", "", stripped)
            for pat in (r"\bdatetime\.now\b", r"\btime\.time\b", r"\bdate\.today\b",
                        r"\bdatetime\.utcnow\b", r"\btime\.monotonic\b"):
                if re.search(pat, stripped):
                    offenders.append(f"{py.name}: {pat}")
        assert not offenders, f"store reads a clock internally: {offenders}"

    def test_timestamps_come_only_from_arguments(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        rec = store.transition(content["plan_id"], 1, "PROPOSED", ts=fixtures.T3)
        assert rec["updated_at"] == fixtures.T3


class TestLookupErrors:
    def test_missing_plan_raises_state_not_found(self, fixtures):
        with pytest.raises(PlanNotFoundError) as exc:
            PlanStore().get_content("PLAN-ABSENT")
        fixtures.validate(exc.value.details, "error_details_state_not_found")

    def test_details_always_carry_all_three_required_fields(self, fixtures):
        with pytest.raises(PlanNotFoundError) as exc:
            PlanStore().get_content("PLAN-ABSENT", 7)
        d = exc.value.details
        assert set(d) == {"lookup_kind", "requested_state_id", "requested_plan_id"}
        assert d["requested_plan_id"] == "PLAN-ABSENT"
        assert d["requested_state_id"] is None  # plan lookup, not workflow state

    def test_missing_version_raises(self, store, content):
        with pytest.raises(PlanNotFoundError):
            store.get_content(content["plan_id"], 99)

    def test_missing_lifecycle_raises_with_its_own_lookup_kind(self, store, content):
        with pytest.raises(PlanNotFoundError) as exc:
            store.get_lifecycle(content["plan_id"], 1)
        assert exc.value.details["lookup_kind"] == "plan_lifecycle"

    def test_has_reports_absence_without_raising(self, store, content):
        assert store.has(content["plan_id"]) is True
        assert store.has("PLAN-ABSENT") is False
        assert store.has(content["plan_id"], 99) is False

    def test_put_content_rejects_a_non_dict(self):
        with pytest.raises(TypeError):
            PlanStore().put_content("not a dict")

    def test_put_content_rejects_a_missing_key(self):
        """Malformed content is INVALID_INPUT, not STATE_NOT_FOUND (audit F2).

        The first version raised PlanNotFoundError with lookup_kind
        "plan_content_key" — a value outside the schema enum, so an unemittable
        tool_error. Nothing was looked up; the payload was simply invalid.
        """
        with pytest.raises(InvalidContentError) as exc:
            PlanStore().put_content({"plan_id": "X"})
        d = exc.value.details  # must satisfy $defs.error_details_invalid_input
        assert d["rejected_entity_type"] == "plan_content"
        assert exc.value.code == "INVALID_INPUT"
        assert exc.value.retryable is False
