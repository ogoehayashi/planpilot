"""Pin tests for the audit fixes F1, F3, F4, F6, F11.

Each test is named for the finding it pins and fails if the fix regresses. F2 and
F5 are covered by test_errors_schema.py (the ghost test). These cover the
behavioural fixes in plan_store.py and digest.py.

The audit findings were reproduced against clean HEAD before any fix, so these
are not testing imagined defects — see tests/evidence and the review handoff.
"""

from __future__ import annotations

import copy
import json

import pytest

import _fixtures as fixtures
from planpilot.store import LIFECYCLE_STATUSES, PlanStore, canonical_plan_digest
from planpilot.store.digest import OPERATION_SORT_KEY_FIELDS, sort_operations
from planpilot.store.errors import (
    DigestMismatchError,
    IdempotencyConflictError,
    InvalidContentError,
    PlanNotFoundError,
    SchemaViolationError,
    VersionConflictError,
)


# --------------------------------------------------------------------- fixtures

@pytest.fixture
def store():
    return PlanStore()


@pytest.fixture
def content():
    c = fixtures.make_content()
    d = canonical_plan_digest(c)
    c["plan_digest"] = d
    c["engine"]["canonical_plan_hash"] = d
    return c


def _reorder_ops(content):
    """Return a deep copy with operations reversed — same content, different order."""
    c = copy.deepcopy(content)
    c["operations"] = list(reversed(c["operations"]))
    return c


# --------------------------------------------------------- F4/F12: total order

class TestF4SortOperationsIsATotalOrder:
    def test_reordering_tied_operations_does_not_change_the_digest(self, content):
        """The contract key is not a total order; the tiebreaker makes it one.

        Measured before the fix: reordering two ops tied on all five key fields
        changed the digest, breaking plan_store.retention.
        """
        reordered = _reorder_ops(content)
        assert canonical_plan_digest(content) == canonical_plan_digest(reordered)

    def test_sort_operations_is_input_order_independent(self):
        op_a = fixtures.make_operation(operation_no=1, machine_id="M1", order_id="O1",
                                       start_time="2026-09-14T08:00:00+08:00", duration_min=60)
        op_b = fixtures.make_operation(operation_no=1, machine_id="M1", order_id="O1",
                                       start_time="2026-09-14T08:00:00+08:00", duration_min=120)
        s1 = sort_operations([copy.deepcopy(op_a), copy.deepcopy(op_b)])
        s2 = sort_operations([copy.deepcopy(op_b), copy.deepcopy(op_a)])
        assert [o["duration_min"] for o in s1] == [o["duration_min"] for o in s2]

    def test_primary_key_still_governs_the_order(self):
        """The tiebreaker must not override the contract's five-field ordering."""
        early = fixtures.make_operation(start_time="2026-09-14T08:00:00+08:00",
                                        machine_id="M9", order_id="Z", operation_no=9)
        late = fixtures.make_operation(start_time="2026-09-14T09:00:00+08:00",
                                       machine_id="M1", order_id="A", operation_no=1)
        out = sort_operations([late, early])
        assert out[0]["start_time"] == "2026-09-14T08:00:00+08:00"


class TestF12SortOperationsIsTotalOnMalformedKeys:
    """Design decision 4 promises the digest layer never raises on bad input.

    F12: mixed-type sort fields raised TypeError, contradicting that promise.
    A malformed operation must still produce a deterministic digest; schema
    rejection is validate_plan's job, not the digest layer's.
    """

    def _op(self, **over):
        base = fixtures.make_operation(operation_no=1, machine_id="M1",
                                       order_id="O1", start_time="2026-09-14T08:00:00+08:00")
        base.update(over)
        return base

    def test_mixed_int_str_lot_no_does_not_raise(self):
        """The exact F12 reproduction: lot_no 1 vs '1' on otherwise-tied ops."""
        ops = [self._op(lot_no=1), self._op(lot_no="1")]
        out = sort_operations(ops)          # must not raise TypeError
        assert len(out) == 2

    def test_dict_in_a_sort_field_does_not_raise(self):
        ops = [self._op(lot_no=1), self._op(lot_no={"weird": "value"})]
        assert len(sort_operations(ops)) == 2

    def test_none_in_a_sort_field_does_not_raise(self):
        ops = [self._op(lot_no=1), self._op(lot_no=None)]
        assert len(sort_operations(ops)) == 2

    def test_digest_is_computable_on_mixed_type_keys(self):
        """canonical_plan_digest must not raise either — it calls sort_operations."""
        content = fixtures.make_content()
        content["operations"] = [self._op(lot_no=1), self._op(lot_no="1")]
        d = canonical_plan_digest(content)   # must not raise
        assert len(d) == 64

    def test_mixed_type_order_is_deterministic(self):
        """Whatever order it picks, it must pick the SAME order every time."""
        ops = [self._op(lot_no=1), self._op(lot_no="1"), self._op(lot_no=None)]
        d1 = [o["lot_no"] for o in sort_operations(ops)]
        d2 = [o["lot_no"] for o in sort_operations(list(reversed(ops)))]
        # canonical-JSON tiebreaker makes even cross-type ties order-independent
        assert d1 == d2

    def test_type_rank_uses_equality_not_identity(self):
        """Guard against `rank is FALLBACK` regressions (works only by int caching).

        _total_key must route a dict to the fallback branch via ==, so a future
        change to the fallback value cannot silently break dispatch.
        """
        from planpilot.store.digest import _total_key, _TYPE_RANK_FALLBACK
        rank, _ = _total_key({"x": 1})
        assert rank == _TYPE_RANK_FALLBACK
        # a well-formed value keeps its own type rank, not the fallback
        assert _total_key(7)[0] != _TYPE_RANK_FALLBACK
        assert _total_key("s")[0] != _TYPE_RANK_FALLBACK


# --------------------------------------------------------- F3: idempotent retry

class TestF3IdempotentRetryComparesDigestNotBytes:
    def test_reordering_operations_on_retry_is_idempotent(self, store, content):
        """A retry with operations in a different order is the SAME content.

        The first version compared canonical_json (which does not sort operations),
        so a logically-identical retry raised IdempotencyConflictError.
        """
        d = store.put_content(content)
        reordered = _reorder_ops(content)
        # same digest, so the rewrite is an idempotent no-op, not a conflict
        assert store.put_content(reordered) == d
        assert len(store) == 1

    def test_genuinely_different_content_still_conflicts(self, store, content):
        store.put_content(content)
        other = copy.deepcopy(content)
        other["kpis"]["on_time_rate"] = 0.42
        d2 = canonical_plan_digest(other)
        other["plan_digest"] = d2
        other["engine"]["canonical_plan_hash"] = d2
        with pytest.raises(IdempotencyConflictError):
            store.put_content(other)

    def test_exact_same_bytes_still_idempotent(self, store, content):
        d = store.put_content(content)
        assert store.put_content(copy.deepcopy(content)) == d


# --------------------------------------------------------- F11: explicit version

class TestF11TransitionRequiresExplicitVersion:
    def test_transition_with_none_version_is_refused(self, store, content):
        """None resolved to max(versions) — could publish an unapproved version.

        A state MUTATION must name the exact version an approval is bound to.
        """
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        with pytest.raises(InvalidContentError) as exc:
            store.transition(content["plan_id"], None, "PUBLISHED", ts=fixtures.T1)
        fixtures.validate(exc.value.details, "error_details_invalid_input")

    def test_transition_with_a_bool_version_is_refused(self, store, content):
        """bool is a subclass of int in Python; True must not be accepted as v1."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        with pytest.raises(InvalidContentError):
            store.transition(content["plan_id"], True, "PUBLISHED", ts=fixtures.T1)

    def test_transition_with_an_explicit_int_still_works(self, store, content):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        rec = store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1)
        assert rec["status"] == "APPROVED"

    def test_reads_may_still_default_to_latest(self, store, content):
        """Only mutations require an explicit version; reads may use None."""
        store.put_content(content)
        assert store.get_content(content["plan_id"])["plan_version"] == 1
        assert store.latest_version(content["plan_id"]) == 1


# --------------------------------------------------------- F6: version reporting

class TestF6VersionReportingCannotMaskDigestMismatch:
    def test_non_int_version_reports_digest_mismatch_not_value_error(self, content):
        """int(plan_version) raised ValueError, masking the real DigestMismatchError.

        A content with a non-numeric plan_version and a wrong digest must raise
        DigestMismatchError — the error the caller actually needs — never
        ValueError. Measured: the fix routes version through _safe_version() so
        reporting cannot fail.
        """
        from planpilot.store.digest import assert_digest_consistent
        broken = copy.deepcopy(content)
        broken["plan_version"] = "not-an-int"
        broken["plan_digest"] = "0" * 64  # well-formed hex, but wrong -> mismatch
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(broken)

    def test_missing_version_reports_digest_mismatch_not_value_error(self, content):
        from planpilot.store.digest import assert_digest_consistent
        broken = copy.deepcopy(content)
        del broken["plan_version"]
        broken["plan_digest"] = "0" * 64
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(broken)

    def test_malformed_digest_reports_invalid_content(self, content):
        """A None digest is structurally invalid, not a mismatch (F5/F6 boundary)."""
        from planpilot.store.digest import assert_digest_consistent
        broken = copy.deepcopy(content)
        broken["plan_digest"] = None
        with pytest.raises(InvalidContentError):
            assert_digest_consistent(broken)


# --------------------------------------------------------- F1: load_state validation

class TestF1LoadStateValidatesLifecycle:
    def _dump(self, store, path):
        return store.dump_state(path)

    def test_round_trip_of_a_valid_state_succeeds(self, store, content, tmp_path):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T1)
        p = tmp_path / "state.json"
        store.dump_state(p)

        fresh = PlanStore()
        fresh.load_state(p)
        assert fresh.get_lifecycle(content["plan_id"], 1)["status"] == "APPROVED"

    def test_load_refuses_a_lifecycle_with_a_fake_status(self, store, content, tmp_path):
        """F1: lifecycle records were restored with NO validation.

        Now caught by the SHAPE layer (STATE_SCHEMA's status enum) rather than by
        the explicit enum check that F1 added — the envelope is validated as a
        whole before any record is inspected. Same code (INVALID_INPUT), earlier
        and stronger: a malformed `updated_at` or an unknown top-level key is
        refused too, which a per-field enum check could not see.
        """
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        p = tmp_path / "state.json"
        store.dump_state(p)

        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["lifecycle"][0]["status"] = "HACKED_PUBLISHED"
        p.write_text(json.dumps(raw), encoding="utf-8")

        fresh = PlanStore()
        with pytest.raises(SchemaViolationError) as exc:
            fresh.load_state(p)
        fixtures.validate(exc.value.details, "error_details_invalid_input")
        assert exc.value.code == "INVALID_INPUT"
        assert len(fresh) == 0, "a refused load must not half-populate the store"

    def test_load_refuses_a_lifecycle_digest_that_disagrees_with_content(self, store, content, tmp_path):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        p = tmp_path / "state.json"
        store.dump_state(p)

        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["lifecycle"][0]["plan_digest"] = "a" * 64  # disagrees with content
        p.write_text(json.dumps(raw), encoding="utf-8")

        fresh = PlanStore()
        with pytest.raises(DigestMismatchError):
            fresh.load_state(p)
        assert len(fresh) == 0

    def test_load_refuses_a_lifecycle_pointing_at_absent_content(self, store, content, tmp_path):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"], ts=fixtures.T0)
        p = tmp_path / "state.json"
        store.dump_state(p)

        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["lifecycle"][0]["plan_version"] = 99  # no such content
        p.write_text(json.dumps(raw), encoding="utf-8")

        fresh = PlanStore()
        with pytest.raises(PlanNotFoundError):
            fresh.load_state(p)
        assert len(fresh) == 0

    def test_load_still_refuses_corrupt_content(self, store, content, tmp_path):
        """The pre-existing content-digest check must still hold."""
        store.put_content(content)
        p = tmp_path / "state.json"
        store.dump_state(p)
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["content"][0]["kpis"]["on_time_rate"] = 0.01  # digest no longer matches
        p.write_text(json.dumps(raw), encoding="utf-8")

        fresh = PlanStore()
        with pytest.raises((DigestMismatchError, InvalidContentError)):
            fresh.load_state(p)
        assert len(fresh) == 0
