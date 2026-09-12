"""Regression tests for the two P0 findings from the second independent audit.

These are the probe payloads from the verification run, converted to pytest. Every
one of them calls `PlanStore` directly — the defect class being pinned is "a check
that only ever exercised the fixture validator", so a test that validates a dict
without writing it to the store proves nothing.

P0-1 — the store never validated content against $defs.plan_content. It checked
  id/version presence and digest self-consistency, so ANY schema-invalid plan was
  accepted as long as the caller re-signed its digest. Confirmed by re-signing five
  mutated plans and watching all five load.

P0-2 — `expected_plan_version` was compared against the caller's own
  `plan_version` argument. Both values came from the caller, so the check was a
  tautology and could not do what its docstring claimed (stop a stale approval set
  from publishing a regenerated plan). Confirmed: with v1 APPROVED and v2 stored,
  `transition(pid, 1, "PUBLISHED", expected_plan_version=1)` published v1.

Also pinned here: the P1 findings in load_state (duplicate keys silently
collapsed, unvalidated supersede events) and the deliberate NON-enforcement in
put_content, so that the distinction between the primitive and the atomic
commit_new_version() cannot be lost by a future refactor.
"""

from __future__ import annotations

import copy
import json

import pytest

import _fixtures as fixtures
from planpilot.store import PlanStore, canonical_plan_digest
from planpilot.store.errors import (
    DigestMismatchError,
    InvalidContentError,
    PlanNotFoundError,
    SchemaViolationError,
    VersionConflictError,
    VersionRouteError,
)


# --------------------------------------------------------------------- fixtures

@pytest.fixture
def store():
    return PlanStore()


@pytest.fixture
def content():
    """A digest-consistent, schema-valid plan_content, ready to store."""
    return resign(fixtures.make_content())


def resign(content: dict) -> dict:
    """Recompute both digest identities after mutating a plan.

    This is what an attacker does, and it is the whole point of P0-1: the digest
    is content-addressed, so re-signing is trivial. Any defence that relies on the
    digest alone is defeated by one function call.
    """
    out = copy.deepcopy(content)
    out.pop("plan_digest", None)
    digest = canonical_plan_digest(out)
    out["plan_digest"] = digest
    if "engine" in out:
        out["engine"]["canonical_plan_hash"] = digest
    return out


def next_version(content: dict, version: int, **overrides) -> dict:
    """A digest-consistent copy at `version`, optionally with changed kpis."""
    out = copy.deepcopy(content)
    out["plan_version"] = version
    for key, value in overrides.items():
        out.setdefault("kpis", {})[key] = value
    return resign(out)


# ============================================================== P0-1: schema gate
#
# Five payloads that the schema rejects but the store accepted, each re-signed so
# the digest check passes. Every one must now be refused by put_content.

class TestP01ContentIsSchemaValidatedOnWrite:
    PAYLOADS = [
        ("unknown top-level field",
         lambda c: c.__setitem__("totally_made_up_field", "x")),
        ("plan_version=True (bool is an int in Python)",
         lambda c: c.__setitem__("plan_version", True)),
        ("kpis.on_time_delivery as a string",
         lambda c: c["kpis"].__setitem__("on_time_delivery", "high")),
        ("required kpis block deleted",
         lambda c: c.pop("kpis", None)),
        ("unknown field nested in operations[0]",
         lambda c: c["operations"][0].__setitem__("made_up_nested", "x")),
    ]

    @pytest.mark.parametrize("label,mutate", PAYLOADS, ids=[p[0] for p in PAYLOADS])
    def test_a_resigned_but_schema_invalid_plan_is_refused(self, store, content, label, mutate):
        bad = resign(content)
        mutate(bad)
        bad = resign(bad)

        # Precondition: the payload really does violate the contract. Without this
        # the test would pass vacuously if the mutation failed to invalidate.
        assert not fixtures.is_valid(bad, "plan_content"), \
            f"payload {label!r} did not actually break the schema"

        with pytest.raises((SchemaViolationError, InvalidContentError)):
            store.put_content(bad)
        assert len(store) == 0, "a refused write must not store anything"

    def test_a_valid_plan_is_still_accepted(self, store, content):
        """The gate must not be a blanket refusal."""
        digest = store.put_content(content)
        assert digest == content["plan_digest"]
        assert len(store) == 1

    def test_the_error_details_are_emittable(self, store, content):
        """F2/F5 class: a refusal must carry contract-shaped details."""
        bad = resign(content)
        bad["totally_made_up_field"] = "x"
        bad = resign(bad)

        with pytest.raises(SchemaViolationError) as exc:
            store.put_content(bad)
        fixtures.validate(exc.value.details, "error_details_invalid_input")
        assert exc.value.code == "INVALID_INPUT"
        assert exc.value.details["rejected_entity_type"] == "plan_content"
        assert exc.value.details["field_errors"], "field_errors must not be empty"

    def test_bool_version_is_refused_before_it_becomes_a_dict_key(self, store, content):
        """hash(True) == hash(1), so a bool version would COLLIDE with version 1.

        That is a key-integrity hazard independent of the schema: put_content(True)
        would overwrite or read back version 1. Rejected on that ground as well.
        """
        bad = resign(content)
        bad["plan_version"] = True
        bad = resign(bad)
        with pytest.raises((InvalidContentError, SchemaViolationError)):
            store.put_content(bad)

        store.put_content(content)          # version 1, legitimately
        assert store.versions(content["plan_id"]) == [1]

    def test_field_errors_are_capped_at_the_schema_maximum(self, store, content):
        """error_details_invalid_input caps field_errors at maxItems 50.

        Reporting more would make the details themselves invalid, so the surplus
        must be dropped by the validator, not by chance.
        """
        bad = resign(content)
        for i in range(60):
            bad[f"junk_field_{i}"] = i
        bad = resign(bad)
        with pytest.raises(SchemaViolationError) as exc:
            store.put_content(bad)
        assert len(exc.value.details["field_errors"]) <= 50
        fixtures.validate(exc.value.details, "error_details_invalid_input")


# ================================================ P0-1 (cont): timestamp format

class TestP01LifecycleTimestampIsValidated:
    """`ts` was stored verbatim into `updated_at` with no format check.

    $defs.plan_lifecycle requires a date-time string. All five payloads below were
    accepted and stored verbatim before the fix.
    """

    BAD_TS = [
        "not a timestamp at all",
        "",
        None,
        12345,
        "2026-99-99T99:99:99Z",
    ]

    @pytest.mark.parametrize("ts", BAD_TS, ids=[repr(t) for t in BAD_TS])
    def test_create_lifecycle_refuses_a_malformed_ts(self, store, content, ts):
        store.put_content(content)
        with pytest.raises(SchemaViolationError):
            store.create_lifecycle(
                content["plan_id"], 1, content["plan_digest"], ts=ts
            )
        # The lifecycle must not exist, even partially.
        with pytest.raises(PlanNotFoundError):
            store.get_lifecycle(content["plan_id"], 1)

    @pytest.mark.parametrize("ts", BAD_TS, ids=[repr(t) for t in BAD_TS])
    def test_transition_refuses_a_malformed_ts(self, store, content, ts):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        with pytest.raises(SchemaViolationError):
            store.transition(content["plan_id"], 1, "PROPOSED", ts=ts)
        # Nothing half-applied: status must be unchanged.
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"
        assert store.get_lifecycle(content["plan_id"], 1)["updated_at"] == fixtures.T0

    def test_a_valid_timestamp_is_accepted(self, store, content):
        store.put_content(content)
        rec = store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                                     ts=fixtures.T0)
        fixtures.validate(rec, "plan_lifecycle")
        assert rec["updated_at"] == fixtures.T0

    def test_transition_does_not_mutate_the_record_before_validation(self, store, content):
        """The first version mutated `record` in place, so a rejected write left
        the stored record half-changed. Candidate-then-replace makes it atomic."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        before = store.get_lifecycle(content["plan_id"], 1)
        with pytest.raises(SchemaViolationError):
            store.transition(content["plan_id"], 1, "APPROVED", ts="garbage")
        assert store.get_lifecycle(content["plan_id"], 1) == before


# ============================================== P0-2: optimistic concurrency

class TestP02StaleVersionCannotBePublished:
    def _store_with_two_versions(self, store, content):
        """v2 is active; v1 is stale but still carries a non-terminal record.

        This fixture used to build "v1 APPROVED, then v2 stored as DRAFT" by
        calling put_content(v2). That state is now UNREACHABLE through the live
        API, and making it unreachable is the point of P1-b: commit_new_version()
        supersedes v1 as it adds v2, and the authority gate refuses to approve a
        version that is not active. Measured, not assumed — see
        _audit_scratch/probe_reachability.py and probe_occ_reachable.py.

        So the fixture builds the strongest stale state that IS still reachable.
        v1 gets content but no lifecycle record, v2 is committed (supersede is
        skipped because there is no status to move), and only then does v1 get a
        DRAFT record. v1 is stale AND non-terminal, which is precisely what lets
        the optimistic-concurrency check be exercised without the terminal check
        firing first — the property the negative control depends on.
        """
        store.put_content(content)
        v2 = next_version(content, 2, on_time_rate=0.6)
        store.commit_new_version(v2, ts=fixtures.T1)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)

        assert store.current_active_version(content["plan_id"]) == 2
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"
        return v2

    def test_the_original_exploit_is_closed(self, store, content):
        """The exact reproduction from the audit: v1 gets PUBLISHED while v2 exists.

        `expected_plan_version=1` matched the caller's own argument, so the
        tautological check passed and a stale version was published.
        """
        self._store_with_two_versions(store, content)
        with pytest.raises(VersionConflictError) as exc:
            store.transition(content["plan_id"], 1, "PUBLISHED", ts=fixtures.T3,
                             expected_plan_version=1)
        fixtures.validate(exc.value.details, "error_details_plan_version_conflict")
        # v1 must be untouched — still DRAFT, never PUBLISHED.
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"

    def test_publishing_a_stale_version_is_refused_even_without_expected(self, store, content):
        """No `expected_plan_version` at all must not reopen the hole.

        The authority check is independent of the caller opting in to concurrency
        control — otherwise omitting the argument bypasses the protection.
        """
        self._store_with_two_versions(store, content)
        with pytest.raises(VersionConflictError):
            store.transition(content["plan_id"], 1, "PUBLISHED", ts=fixtures.T3)
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"

    def test_approving_a_stale_version_is_refused(self, store, content):
        """APPROVED grants authority too, so it is gated the same way."""
        self._store_with_two_versions(store, content)
        with pytest.raises(VersionConflictError):
            store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T3)

    def test_the_active_version_can_still_be_published(self, store, content):
        """The gate must not block legitimate publication of the live version."""
        v2 = self._store_with_two_versions(store, content)
        store.transition(content["plan_id"], 2, "AWAITING_APPROVAL", ts=fixtures.T3,
                         approval_set_id="AS-002")
        rec = store.transition(content["plan_id"], 2, "APPROVED", ts=fixtures.T3)
        assert rec["status"] == "APPROVED"
        rec = store.transition(content["plan_id"], 2, "PUBLISHED", ts=fixtures.T3,
                               published_version=2, expected_plan_version=2)
        assert rec["status"] == "PUBLISHED"
        assert rec["published_version"] == 2

    def test_non_authority_statuses_are_allowed_on_a_stale_version(self, store, content):
        """Deliberately narrow gate: PROPOSED requests authority, it does not confer it.

        Blocking these would break preparing an older draft while a newer version
        exists, which is a legitimate workflow.
        """
        self._store_with_two_versions(store, content)
        rec = store.transition(content["plan_id"], 1, "BLOCKED", ts=fixtures.T3)
        assert rec["status"] == "BLOCKED"

    def test_current_active_version_tracks_the_highest_stored(self, store, content):
        self._store_with_two_versions(store, content)
        assert store.current_active_version(content["plan_id"]) == 2

    def test_current_active_version_raises_for_an_unknown_plan(self, store):
        with pytest.raises(PlanNotFoundError):
            store.current_active_version("NOPE")

    def test_expected_against_the_active_version_succeeds(self, store, content):
        """A caller holding the CURRENT version is not blocked."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        rec = store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                               expected_plan_version=1)
        assert rec["status"] == "APPROVED"

    def test_a_contradictory_expected_version_still_raises(self, store, content):
        """The one thing the old tautological check did catch must still be caught."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        with pytest.raises(VersionConflictError) as exc:
            store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                             expected_plan_version=99)
        fixtures.validate(exc.value.details, "error_details_plan_version_conflict")
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"

    def test_expected_version_is_checked_against_the_active_version_not_the_argument(
        self, store, content
    ):
        """Pins OCC INDEPENDENTLY of the authority gate.

        Added because the negative control escaped without it. Every other P0-2
        test here uses PUBLISHED or APPROVED, which the authority gate blocks on
        its own — so reverting `expected_plan_version != active` back to the
        tautological `!= version` left the suite green and the defect returned.

        BLOCKED grants no authority, so only the OCC check can catch this. The
        caller asserts "the active version is 1"; the store knows it is 2; that
        expectation is wrong and must conflict regardless of the target status.
        """
        self._store_with_two_versions(store, content)
        assert store.current_active_version(content["plan_id"]) == 2

        with pytest.raises(VersionConflictError) as exc:
            store.transition(content["plan_id"], 1, "BLOCKED", ts=fixtures.T3,
                             expected_plan_version=1)
        fixtures.validate(exc.value.details, "error_details_plan_version_conflict")
        assert exc.value.details["expected_plan_version"] == 1
        assert exc.value.details["actual_plan_version"] == 2
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"

    def test_expected_version_against_the_active_version_is_accepted(self, store, content):
        """The mirror case: a correct expectation on a stale version is fine."""
        self._store_with_two_versions(store, content)
        rec = store.transition(content["plan_id"], 1, "BLOCKED", ts=fixtures.T3,
                               expected_plan_version=2)
        assert rec["status"] == "BLOCKED"


# ============================================ P0-2: supersede and continuity

class TestP02CommitNewVersionIsAtomic:
    def test_commit_supersedes_the_previous_version(self, store, content):
        """plan_store.versioning: a superseded version moves to SUPERSEDED.

        Before commit_new_version() existed, writing v2 left v1 sitting at
        APPROVED with drain_superseded() empty — the contract's invalidation
        requirement was simply not implemented on the write path.
        """
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                         approval_set_id="AS-001")

        v2 = next_version(content, 2, on_time_rate=0.6)
        rec = store.commit_new_version(v2, ts=fixtures.T2)

        assert rec["status"] == "DRAFT"
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "SUPERSEDED"
        events = store.drain_superseded()
        assert len(events) == 1
        assert events[0]["plan_version"] == 1
        assert events[0]["approval_set_id"] == "AS-001"
        assert events[0]["invalidation_cause"] == "superseded_version"

    def test_invalidation_cause_is_a_contract_member(self, store, content):
        """The cause comes from a closed enum, so it cannot be a free string."""
        from planpilot.store import INVALIDATION_CAUSES
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                 ts=fixtures.T1)
        event = store.drain_superseded()[0]
        assert event["invalidation_cause"] in INVALIDATION_CAUSES

    def test_the_superseded_version_cannot_come_back(self, store, content):
        """SUPERSEDED is terminal: no transition may resurrect or publish it."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                 ts=fixtures.T1)
        from planpilot.store.errors import TransitionNotAllowedError
        for status in ("DRAFT", "APPROVED", "PUBLISHED", "PROPOSED"):
            with pytest.raises(TransitionNotAllowedError):
                store.transition(content["plan_id"], 1, status, ts=fixtures.T2)
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "SUPERSEDED"

    def test_a_skipped_version_number_is_refused(self, store, content):
        """new_version must be latest + 1."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        v7 = next_version(content, 7, on_time_rate=0.6)
        with pytest.raises(VersionConflictError):
            store.commit_new_version(v7, ts=fixtures.T1)
        assert store.versions(content["plan_id"]) == [1], "nothing was written"

    def test_a_repeated_version_number_is_refused(self, store, content):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        again = next_version(content, 1, on_time_rate=0.6)
        with pytest.raises(VersionConflictError):
            store.commit_new_version(again, ts=fixtures.T1)

    def test_the_first_version_needs_no_continuity(self, store, content):
        """First write accepts any contract-legal non-negative version."""
        first = next_version(content, 5)
        rec = store.commit_new_version(first, ts=fixtures.T0)
        assert rec["plan_version"] == 5
        assert store.drain_superseded() == []

    def test_a_failed_commit_leaves_the_store_untouched(self, store, content):
        """Any step failing must roll the whole thing back."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        before_content = dict(store._content)
        before_lifecycle = dict(store._lifecycle)

        bad = next_version(content, 2, on_time_rate=0.6)
        bad["totally_made_up_field"] = "x"
        bad = resign(bad)

        with pytest.raises((SchemaViolationError, InvalidContentError)):
            store.commit_new_version(bad, ts=fixtures.T1)

        assert store._content == before_content
        assert store._lifecycle == before_lifecycle
        assert store.versions(content["plan_id"]) == [1]
        assert store.drain_superseded() == []

    def test_a_bad_timestamp_rolls_the_commit_back(self, store, content):
        """The lifecycle step can fail after content was written — rollback matters."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        with pytest.raises(SchemaViolationError):
            store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                     ts="not a timestamp")
        assert store.versions(content["plan_id"]) == [1]
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"

    def test_put_content_refuses_to_add_a_version_at_all(self, store, content):
        """P1-b: put_content() writes the FIRST version only.

        These two tests used to pin the opposite — that put_content() alone
        neither supersedes nor enforces continuity — as a deliberate design
        split. That split WAS the defect. Writing v2 through put_content() moved
        current_active_version() to 2 while v1 kept its APPROVED status and its
        live approval set, which is exactly the state plan_store.versioning
        forbids, and it held only for callers who happened to pick
        commit_new_version(). A "low-level primitive" that lets callers produce
        a forbidden state is not a primitive, it is a hole.

        Continuity is still enforced, in the one place that can enforce it
        together with the supersede: test_a_skipped_version_number_is_refused.
        """
        store.put_content(content)
        with pytest.raises(VersionRouteError) as exc:
            store.put_content(next_version(content, 2, on_time_rate=0.6))
        assert exc.value.existing_versions == [1]
        assert exc.value.plan_version == 2
        # nothing was written
        assert store.versions(content["plan_id"]) == [1]
        assert not store.has(content["plan_id"], 2)

    def test_the_refusal_names_the_correct_route(self, store, content):
        """The message must tell the caller what to do instead."""
        store.put_content(content)
        with pytest.raises(VersionRouteError) as exc:
            store.put_content(next_version(content, 2, on_time_rate=0.6))
        assert "commit_new_version" in str(exc.value)

    def test_put_content_is_still_idempotent_for_the_version_it_wrote(self, store, content):
        """Gate 4 must not break retries of the SAME version.

        The route check sits after the idempotency branch on purpose: a retried
        write of v1 finds its key present and returns the digest, never reaching
        the "plan already has versions" refusal.
        """
        store.put_content(content)
        assert store.put_content(content) == content["plan_digest"]
        assert store.versions(content["plan_id"]) == [1]

    def test_a_stale_version_can_never_hold_authority_through_the_live_api(
        self, store, content
    ):
        """The invariant P1-b exists to guarantee, end to end.

        Before the fix this was reachable: put_content(v2) made v2 active while
        v1 stayed APPROVED. Now every route to a stale-but-authoritative version
        is closed — commit_new_version() supersedes as it adds, and the authority
        gate refuses to approve a non-active version.
        """
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T2)

        store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                 ts=fixtures.T2)

        assert store.current_active_version(content["plan_id"]) == 2
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == "SUPERSEDED"
        # and the invalidation event was recorded for the approval service
        events = store.drain_superseded()
        assert len(events) == 1
        assert events[0]["approval_set_id"] == "AS-001"


# ==================================================== P1: load_state integrity

class TestP1LoadStateRejectsAForgedDump:
    def _dump(self, store, content, tmp_path, with_second_version=False):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1,
                         approval_set_id="AS-001")
        if with_second_version:
            v2 = next_version(content, 2, on_time_rate=0.6)
            store.commit_new_version(v2, ts=fixtures.T2)
        path = tmp_path / "state.json"
        store.dump_state(path)
        return path

    def _rewrite(self, path, mutate):
        raw = json.loads(path.read_text(encoding="utf-8"))
        mutate(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_a_valid_dump_round_trips(self, tmp_path, content, store):
        path = self._dump(store, content, tmp_path)
        fresh = PlanStore()
        fresh.load_state(path)
        assert fresh.get_lifecycle(content["plan_id"], 1)["status"] == "APPROVED"

    def test_duplicate_content_key_is_refused_not_collapsed(self, tmp_path, content, store):
        """A dict comprehension kept only the LAST duplicate. That is a swap, not
        a load: the surviving record is whichever appeared later in the file."""
        path = self._dump(store, content, tmp_path)

        def duplicate(raw):
            other = resign(next_version(raw["content"][0], 1, on_time_rate=0.1))
            raw["content"].append(other)

        self._rewrite(path, duplicate)
        fresh = PlanStore()
        with pytest.raises(InvalidContentError):
            fresh.load_state(path)
        assert len(fresh) == 0

    def test_duplicate_lifecycle_key_is_refused(self, tmp_path, content, store):
        path = self._dump(store, content, tmp_path)

        def duplicate(raw):
            forged = dict(raw["lifecycle"][0])
            forged["status"] = "PUBLISHED"
            forged["published_version"] = 1
            raw["lifecycle"].append(forged)

        self._rewrite(path, duplicate)
        fresh = PlanStore()
        with pytest.raises(InvalidContentError):
            fresh.load_state(path)
        assert len(fresh) == 0

    def test_a_fabricated_supersede_event_is_refused(self, tmp_path, content, store):
        """An event for a plan that does not exist would be handed to the
        approval service, which would invalidate a real approval set on it."""
        path = self._dump(store, content, tmp_path)

        def forge(raw):
            raw["superseded_events"].append({
                "plan_id": "PLAN-THAT-NEVER-EXISTED",
                "plan_version": 1,
                "plan_digest": "a" * 64,
                "approval_set_id": "AS-999",
                "superseded_at": fixtures.T2,
                "invalidation_cause": "superseded_version",
            })

        self._rewrite(path, forge)
        fresh = PlanStore()
        with pytest.raises(PlanNotFoundError):
            fresh.load_state(path)
        assert len(fresh) == 0
        assert fresh.drain_superseded() == []

    def test_an_event_with_the_wrong_digest_is_refused(self, tmp_path, content, store):
        path = self._dump(store, content, tmp_path, with_second_version=True)

        def corrupt(raw):
            raw["superseded_events"][0]["plan_digest"] = "f" * 64

        self._rewrite(path, corrupt)
        fresh = PlanStore()
        with pytest.raises(DigestMismatchError):
            fresh.load_state(path)
        assert len(fresh) == 0

    def test_an_unknown_top_level_key_is_refused(self, tmp_path, content, store):
        """The envelope is closed: a dump with extra keys was not written by
        dump_state, so its origin is unknown."""
        path = self._dump(store, content, tmp_path)
        self._rewrite(path, lambda raw: raw.__setitem__("injected", {"x": 1}))
        fresh = PlanStore()
        with pytest.raises(SchemaViolationError):
            fresh.load_state(path)
        assert len(fresh) == 0

    def test_a_missing_required_section_is_refused(self, tmp_path, content, store):
        path = self._dump(store, content, tmp_path)
        self._rewrite(path, lambda raw: raw.pop("superseded_events"))
        fresh = PlanStore()
        with pytest.raises(SchemaViolationError):
            fresh.load_state(path)

    def test_a_malformed_event_timestamp_is_refused(self, tmp_path, content, store):
        path = self._dump(store, content, tmp_path, with_second_version=True)
        self._rewrite(path,
                      lambda raw: raw["superseded_events"][0].__setitem__(
                          "superseded_at", "yesterday"))
        fresh = PlanStore()
        with pytest.raises(SchemaViolationError):
            fresh.load_state(path)

    def test_a_refused_load_leaves_a_populated_store_unchanged(self, tmp_path, content, store):
        """load_state on a live store must not half-clear it."""
        path = self._dump(store, content, tmp_path)
        self._rewrite(path, lambda raw: raw.__setitem__("injected", 1))

        live = PlanStore()
        live.put_content(content)
        live.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                              ts=fixtures.T0)
        with pytest.raises(SchemaViolationError):
            live.load_state(path)
        assert live.get_lifecycle(content["plan_id"], 1)["status"] == "DRAFT"
        assert len(live) == 1
