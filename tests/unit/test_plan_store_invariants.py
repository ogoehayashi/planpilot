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

from planpilot.store import (
    CREATION_STATUS,
    LIFECYCLE_STATUSES,
    PlanStore,
    canonical_plan_digest,
)
from planpilot.store.errors import (
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

    def test_a_new_version_of_the_same_plan_is_allowed(self, store, content, fixtures):
        """versioning: plan_version increments on every regeneration.

        This test used to add v2 with put_content() and assert the store held
        [1, 2]. That assertion encoded the P1-b defect: put_content() moved the
        active version to 2 while leaving v1 exactly as it was, so a plan could
        have two live versions and an approval set bound to one that was no
        longer current. Adding a version is commit_new_version()'s job, because
        that is the only method that does the rest of the transition too.
        """
        v2 = copy.deepcopy(content)
        v2["plan_version"] = 2
        v2["kpis"]["on_time_rate"] = 0.6
        d = canonical_plan_digest(v2)
        v2["plan_digest"] = d
        v2["engine"]["canonical_plan_hash"] = d
        store.commit_new_version(v2, ts=fixtures.T0)
        assert store.versions(content["plan_id"]) == [1, 2]
        assert store.latest_version(content["plan_id"]) == 2
        assert store.current_active_version(content["plan_id"]) == 2


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

    def test_unknown_field_is_rejected_by_the_schema(self, store, content, fixtures):
        """plan_content has additionalProperties: false.

        This test originally asserted only `not fixtures.is_valid(polluted, ...)`.
        That is the exact defect the second audit reported as P0-1: it claimed the
        store rejects unknown fields while never calling the store. The store at
        the time accepted them, because it checked digest self-consistency and
        nothing else — and a fabricated plan can always be re-signed.

        Now it asserts BOTH halves: the payload violates the contract, and
        put_content refuses it. The precondition matters — without it a payload
        that stopped being invalid would make the store assertion pass vacuously.
        """
        polluted = copy.deepcopy(content)
        polluted["status"] = "PUBLISHED"      # belongs to plan_lifecycle, not here
        assert not fixtures.is_valid(polluted, "plan_content"), \
            "payload no longer violates additionalProperties:false; test is vacuous"

        # The polluted copy is still digest-consistent, so ONLY the schema gate
        # can reject it. That is what makes this a real P0-1 regression test.
        before = store.get_content(content["plan_id"], 1)
        with pytest.raises(SchemaViolationError):
            store.put_content(polluted)
        # The `store` fixture already holds v1, so the assertion is that the
        # refused write changed nothing — not that the store is empty.
        assert store.get_content(content["plan_id"], 1) == before
        assert "status" not in store.get_content(content["plan_id"], 1)

    def test_a_resigned_forged_plan_is_still_rejected(self, store, content, fixtures):
        """Re-signing the forgery must not help.

        The digest is content-addressed, so an attacker who adds a field can
        recompute plan_digest and engine.canonical_plan_hash to match. Any defence
        resting on the digest alone is defeated by one function call — which is
        precisely why the schema gate exists separately from it.
        """
        forged = copy.deepcopy(content)
        forged["injected_field"] = "attacker-controlled"
        digest = canonical_plan_digest(forged)
        forged["plan_digest"] = digest
        forged["engine"]["canonical_plan_hash"] = digest

        # digest identities hold, so assert_digest_consistent would pass
        assert forged["plan_digest"] == canonical_plan_digest(forged)
        before = store.get_content(content["plan_id"], 1)
        with pytest.raises(SchemaViolationError):
            store.put_content(forged)
        assert store.get_content(content["plan_id"], 1) == before
        assert "injected_field" not in store.get_content(content["plan_id"], 1)


class TestLifecycle:
    def test_create_requires_existing_content(self, content):
        with pytest.raises(PlanNotFoundError):
            PlanStore().create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts="2026-09-14T08:00:00+08:00")

    def test_create_rejects_a_digest_that_does_not_match_the_stored_content(self, store, content):
        with pytest.raises(DigestMismatchError):
            store.create_lifecycle(content["plan_id"], 1, "f" * 64, ts="2026-09-14T08:00:00+08:00")

    def test_create_lifecycle_cannot_mint_an_authority_status(self, store, content, fixtures):
        """P0-bis: create_lifecycle() takes no status, so it cannot grant authority.

        This test replaced `test_create_rejects_an_unknown_status`, which asserted
        that `status="PUBLISHING"` was refused. That assertion became meaningless
        when the parameter was removed — but removing the parameter is the fix,
        and a fix that nothing pins will be reverted by anyone who finds the
        signature inconvenient.

        So this pins the signature itself, and the three ways it could be
        weakened: re-adding `status`, re-adding `published_version`, or
        re-adding `approval_set_id`. Each of those reopens the bypass, because
        the store would again be able to write an authority-bearing lifecycle
        record through a path that never consults current_active_version().

        Unknown-status rejection is still covered, on the path that can still
        receive a status: test_transition_rejects_an_unknown_status.
        """
        import inspect

        params = set(inspect.signature(PlanStore.create_lifecycle).parameters)
        assert params == {"self", "plan_id", "plan_version", "plan_digest", "ts"}, (
            f"create_lifecycle grew parameters {params - {'self','plan_id','plan_version','plan_digest','ts'}}; "
            f"a status/approval parameter here bypasses transition()'s version gate (P0-bis)"
        )

        rec = store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                                     ts=fixtures.T0)
        assert rec["status"] == "DRAFT"
        assert rec["status"] == CREATION_STATUS
        assert rec["approval_set_id"] is None, "creation must not bind an approval set"
        assert rec["published_version"] is None, "creation must not publish"
        fixtures.validate(rec, "plan_lifecycle")

    def test_creating_lifecycle_on_a_stale_version_still_grants_nothing(self, store, content, fixtures):
        """The P0-bis exploit, replayed: v2 exists, so v1 is stale.

        Before the fix, `create_lifecycle(v1, status="PUBLISHED",
        published_version=1)` was ACCEPTED while transition() refused the same
        move. Now creation cannot express that move at all, and the record it
        does create carries no authority.
        """
        # Build v2 the way this file builds content elsewhere: make_content()
        # then re-sign, since a v2 whose digest still described v1 would fail the
        # digest gate and prove nothing about the version gate.
        draft2 = fixtures.make_content(plan_version=2)
        v2 = fixtures.make_content(plan_version=2,
                                   digest=canonical_plan_digest(draft2))
        store.commit_new_version(v2, ts=fixtures.T1)
        assert store.current_active_version(content["plan_id"]) == 2

        rec = store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                                     ts=fixtures.T0)
        assert rec["status"] == "DRAFT"
        assert rec["published_version"] is None
        # and the authority route is still closed
        with pytest.raises(VersionConflictError):
            store.transition(content["plan_id"], 1, "PUBLISHED", ts=fixtures.T1)

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
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1)
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
        store.transition(content["plan_id"], 1, "PROPOSED", ts=fixtures.T1)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1)
        store.transition(content["plan_id"], 1, "PUBLISHED", ts=fixtures.T1,
                         published_version=1)
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
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001", expected_plan_version=1)
        rec = store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                               expected_plan_version=1)
        assert rec["status"] == "APPROVED"

    def test_stale_approval_cannot_publish_a_regenerated_plan(self, store, content, fixtures):
        """security_controls.approvals_bound_to_plan_version_and_digest, exercised.

        A caller holding `expected_plan_version=1` cannot drive the plan once v2
        is the active version. The expectation is compared with the store's own
        notion of which version is live, which is what P0-2 fixed.

        v1 ends SUPERSEDED here rather than APPROVED, and that is the fix rather
        than a loss: commit_new_version() retires the version it replaces, so a
        stale version holding authority is no longer reachable through the live
        API at all (audit finding P1-b).
        """
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001", expected_plan_version=1)
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                         expected_plan_version=1)

        v2 = copy.deepcopy(content)
        v2["plan_version"] = 2
        v2["kpis"]["on_time_rate"] = 0.6
        d2 = canonical_plan_digest(v2)
        v2["plan_digest"] = d2
        v2["engine"]["canonical_plan_hash"] = d2
        store.commit_new_version(v2, ts=fixtures.T2)

        # a caller still holding version 1 cannot drive version 2
        with pytest.raises(VersionConflictError):
            store.transition(content["plan_id"], 2, "PUBLISHED", ts=fixtures.T2,
                             expected_plan_version=1)
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "SUPERSEDED"
        assert store.get_lifecycle(content["plan_id"], 2)["status"] == "DRAFT"

    def test_transition_needs_a_lifecycle_record_before_it_checks_versions(self, store, content, fixtures):
        """The ordering the test above depends on, pinned on its own terms.

        Reached by giving v1 content but no lifecycle record, then committing v2:
        commit_new_version() skips the supersede when the previous version has no
        record (there is no status to move), so v1 keeps content and gains
        nothing else. transition() must then report the missing record rather
        than a version conflict, because you cannot transition what is not there.
        """
        v2 = copy.deepcopy(content)
        v2["plan_version"] = 2
        v2["kpis"]["on_time_rate"] = 0.6
        d2 = canonical_plan_digest(v2)
        v2["plan_digest"] = d2
        v2["engine"]["canonical_plan_hash"] = d2
        store.commit_new_version(v2, ts=fixtures.T1)   # v1: content only, no lifecycle
        assert (content["plan_id"], 1) not in store._lifecycle
        assert store.current_active_version(content["plan_id"]) == 2

        with pytest.raises(PlanNotFoundError):
            store.transition(content["plan_id"], 1, "PUBLISHED", ts=fixtures.T2,
                             expected_plan_version=1)


class TestSupersede:
    def test_supersede_sets_status_and_records_an_event(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.supersede(content["plan_id"], 1, ts=fixtures.T2)

        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "SUPERSEDED"
        events = store.pending_superseded()
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

    def test_pending_is_retried_until_acknowledged(self, store, content, fixtures):
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.supersede(content["plan_id"], 1, ts=fixtures.T1)
        event = store.pending_superseded()[0]
        assert store.pending_superseded() == [event], "unacknowledged delivery is retried"
        store.acknowledge_superseded(
            event["plan_id"], event["plan_version"], event["plan_digest"],
            event["approval_set_id"], ts=fixtures.T2,
        )
        assert store.pending_superseded() == []

    def test_supersede_is_not_idempotent(self, store, content, fixtures):
        """A second call must raise, not double-record the event.

        Supersede history is immutable and unique; delivery retries are made safe
        by consumer idempotency plus explicit acknowledgement.
        """
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.supersede(content["plan_id"], 1, ts=fixtures.T1)
        event = store.pending_superseded()[0]
        store.acknowledge_superseded(
            event["plan_id"], event["plan_version"], event["plan_digest"],
            event["approval_set_id"], ts=fixtures.T2,
        )
        with pytest.raises(TransitionNotAllowedError):
            store.supersede(content["plan_id"], 1, ts=fixtures.T2)
        assert store.pending_superseded() == [], "no second event may be recorded"

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
