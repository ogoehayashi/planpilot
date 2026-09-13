"""Regression tests for the fourth independent audit.

These cases were all reproduced against fc908d7 while its 384-test suite was
green.  They exercise public PlanStore methods and persisted bytes, not helpers.
"""

from __future__ import annotations

import copy
import json

import pytest

import _fixtures as fixtures
from planpilot.store import PlanStore, canonical_plan_digest
from planpilot.store.errors import (
    InvalidContentError,
    StoreInvariantError,
    TransitionNotAllowedError,
    VersionConflictError,
)


def resign(content: dict) -> dict:
    out = copy.deepcopy(content)
    out.pop("plan_digest", None)
    digest = canonical_plan_digest(out)
    out["plan_digest"] = digest
    out["engine"]["canonical_plan_hash"] = digest
    return out


def content(version: int = 1, rate: float = 0.5) -> dict:
    return resign(
        fixtures.make_content(
            plan_version=version,
            kpis=fixtures.make_kpis(on_time_rate=rate),
        )
    )


def store_at_status(status: str) -> tuple[PlanStore, dict]:
    """Build each lifecycle status through supported public routes."""
    store = PlanStore()
    first = content(1)
    store.put_content(first)
    store.create_lifecycle(first["plan_id"], 1, first["plan_digest"], fixtures.T0)
    if status == "DRAFT":
        return store, first
    if status in {"PROPOSED", "BLOCKED"}:
        store.transition(first["plan_id"], 1, status, fixtures.T1)
        return store, first
    if status == "SUPERSEDED":
        store.supersede(first["plan_id"], 1, fixtures.T1)
        return store, first
    store.transition(
        first["plan_id"], 1, "AWAITING_APPROVAL", fixtures.T1,
        approval_set_id="AS-001",
    )
    if status == "AWAITING_APPROVAL":
        return store, first
    store.transition(first["plan_id"], 1, "APPROVED", fixtures.T2)
    if status == "APPROVED":
        return store, first
    if status == "PUBLISHED":
        store.transition(
            first["plan_id"], 1, "PUBLISHED", fixtures.T3,
            published_version=1,
        )
        return store, first
    raise AssertionError(f"unsupported test status {status!r}")


def superseded_store(*, with_approval: bool = True) -> tuple[PlanStore, dict]:
    store = PlanStore()
    first = content(1)
    store.put_content(first)
    store.create_lifecycle(first["plan_id"], 1, first["plan_digest"], fixtures.T0)
    if with_approval:
        store.transition(
            first["plan_id"], 1, "AWAITING_APPROVAL", fixtures.T1,
            approval_set_id="AS-001",
        )
        store.transition(first["plan_id"], 1, "APPROVED", fixtures.T2)
    store.commit_new_version(content(2, 0.6), fixtures.T3)
    return store, first


def raw_dump(store: PlanStore, tmp_path) -> tuple[object, dict]:
    path = tmp_path / "state.json"
    store.dump_state(path)
    return path, json.loads(path.read_text(encoding="utf-8"))


def write_raw(path, raw: dict) -> None:
    path.write_text(json.dumps(raw), encoding="utf-8")


class TestDurableSupersedeOutbox:
    def test_unacknowledged_event_is_retried(self):
        store, _ = superseded_store()
        first = store.pending_superseded()
        second = store.pending_superseded()
        assert len(first) == 1
        assert second == first
        assert second is not first

    def test_acknowledgement_hides_pending_but_keeps_history(self, tmp_path):
        store, _ = superseded_store()
        event = store.pending_superseded()[0]
        ack = store.acknowledge_superseded(
            event["plan_id"], event["plan_version"], event["plan_digest"],
            event["approval_set_id"], fixtures.T3,
        )
        assert ack["acknowledged_at"] == fixtures.T3
        assert store.pending_superseded() == []

        path, raw = raw_dump(store, tmp_path)
        assert raw["superseded_events"] == [event]
        assert len(raw["superseded_acks"]) == 1

        restored = PlanStore()
        restored.load_state(path)
        assert restored.pending_superseded() == []

    def test_crash_before_ack_keeps_event_pending_after_reload(self, tmp_path):
        store, _ = superseded_store()
        path, _ = raw_dump(store, tmp_path)
        restored = PlanStore()
        restored.load_state(path)
        assert len(restored.pending_superseded()) == 1

    def test_acknowledgement_is_idempotent(self):
        store, _ = superseded_store()
        event = store.pending_superseded()[0]
        args = (
            event["plan_id"], event["plan_version"], event["plan_digest"],
            event["approval_set_id"], fixtures.T3,
        )
        assert store.acknowledge_superseded(*args) == store.acknowledge_superseded(*args)

    def test_wrong_ack_binding_is_refused(self):
        store, _ = superseded_store()
        event = store.pending_superseded()[0]
        with pytest.raises(StoreInvariantError):
            store.acknowledge_superseded(
                event["plan_id"], event["plan_version"], event["plan_digest"],
                "AS-OTHER", fixtures.T3,
            )

    def test_ack_before_event_is_refused(self):
        store, _ = superseded_store()
        event = store.pending_superseded()[0]
        with pytest.raises(StoreInvariantError):
            store.acknowledge_superseded(
                event["plan_id"], event["plan_version"], event["plan_digest"],
                event["approval_set_id"], fixtures.T0,
            )


class TestPersistenceRelationships:
    @pytest.mark.parametrize("field,value", [
        ("invalidation_cause", "kpi_changed"),
        ("superseded_at", fixtures.T0),
    ])
    def test_forged_event_semantics_are_refused(self, tmp_path, field, value):
        store, _ = superseded_store()
        path, raw = raw_dump(store, tmp_path)
        raw["superseded_events"][0][field] = value
        write_raw(path, raw)
        with pytest.raises(InvalidContentError):
            PlanStore().load_state(path)

    def test_superseded_without_history_is_refused_even_without_approval(self, tmp_path):
        store, _ = superseded_store(with_approval=False)
        path, raw = raw_dump(store, tmp_path)
        assert raw["superseded_events"][0]["approval_set_id"] is None
        raw["superseded_events"] = []
        write_raw(path, raw)
        with pytest.raises(InvalidContentError):
            PlanStore().load_state(path)

    def test_duplicate_ack_is_refused(self, tmp_path):
        store, _ = superseded_store()
        event = store.pending_superseded()[0]
        store.acknowledge_superseded(
            event["plan_id"], event["plan_version"], event["plan_digest"],
            event["approval_set_id"], fixtures.T3,
        )
        path, raw = raw_dump(store, tmp_path)
        raw["superseded_acks"].append(copy.deepcopy(raw["superseded_acks"][0]))
        write_raw(path, raw)
        with pytest.raises(InvalidContentError):
            PlanStore().load_state(path)

    def test_ack_without_event_is_refused(self, tmp_path):
        store, _ = superseded_store()
        event = store.pending_superseded()[0]
        store.acknowledge_superseded(
            event["plan_id"], event["plan_version"], event["plan_digest"],
            event["approval_set_id"], fixtures.T3,
        )
        path, raw = raw_dump(store, tmp_path)
        raw["superseded_events"] = []
        raw["lifecycle"] = [lc for lc in raw["lifecycle"] if lc["plan_version"] != 1]
        write_raw(path, raw)
        with pytest.raises(InvalidContentError):
            PlanStore().load_state(path)

    def test_non_contiguous_versions_are_refused(self, tmp_path):
        store = PlanStore()
        store.put_content(content(1))
        path, raw = raw_dump(store, tmp_path)
        raw["content"].append(content(42, 0.6))
        write_raw(path, raw)
        with pytest.raises(InvalidContentError) as exc:
            PlanStore().load_state(path)
        assert "non-contiguous" in str(exc.value)

    def test_old_dump_without_ack_section_remains_readable(self, tmp_path):
        store = PlanStore()
        store.put_content(content(1))
        path, raw = raw_dump(store, tmp_path)
        del raw["superseded_acks"]
        write_raw(path, raw)
        restored = PlanStore()
        restored.load_state(path)
        assert restored.versions(content(1)["plan_id"]) == [1]


class TestLifecycleAuthority:
    @pytest.mark.parametrize(
        "status",
        [
            "DRAFT", "PROPOSED", "BLOCKED", "AWAITING_APPROVAL",
            "APPROVED", "PUBLISHED", "SUPERSEDED",
        ],
    )
    def test_public_transition_cannot_bypass_atomic_supersede(self, status):
        store, first = store_at_status(status)
        before = store.get_lifecycle(first["plan_id"], 1)
        pending_before = store.pending_superseded()
        with pytest.raises(TransitionNotAllowedError):
            store.transition(first["plan_id"], 1, "SUPERSEDED", fixtures.T3)
        assert store.get_lifecycle(first["plan_id"], 1) == before
        assert store.pending_superseded() == pending_before

    @pytest.mark.parametrize(
        "status",
        [
            "DRAFT", "PROPOSED", "BLOCKED", "AWAITING_APPROVAL",
            "APPROVED", "PUBLISHED", "SUPERSEDED",
        ],
    )
    def test_every_publicly_reachable_status_round_trips(self, status, tmp_path):
        store, _ = store_at_status(status)
        first_path = tmp_path / f"{status.lower()}-first.json"
        second_path = tmp_path / f"{status.lower()}-second.json"
        first_dump = store.dump_state(first_path)
        restored = PlanStore()
        restored.load_state(first_path)
        second_dump = restored.dump_state(second_path)
        assert second_dump == first_dump

    @pytest.mark.parametrize("approval_set_id", [None, "AS-STALE"])
    def test_stale_version_cannot_enter_awaiting_approval(self, approval_set_id):
        store = PlanStore()
        first = content(1)
        store.put_content(first)
        store.commit_new_version(content(2, 0.6), fixtures.T1)
        store.create_lifecycle(first["plan_id"], 1, first["plan_digest"], fixtures.T2)
        with pytest.raises(VersionConflictError):
            store.transition(
                first["plan_id"], 1, "AWAITING_APPROVAL", fixtures.T3,
                approval_set_id=approval_set_id,
            )

    def test_draft_cannot_jump_directly_to_published(self):
        store = PlanStore()
        first = content(1)
        store.put_content(first)
        store.create_lifecycle(first["plan_id"], 1, first["plan_digest"], fixtures.T0)
        with pytest.raises(TransitionNotAllowedError):
            store.transition(
                first["plan_id"], 1, "PUBLISHED", fixtures.T1,
                approval_set_id="AS-001", published_version=1,
                expected_plan_version=1,
            )

    def test_published_version_must_match_plan_version(self):
        store = PlanStore()
        first = content(1)
        store.put_content(first)
        store.create_lifecycle(first["plan_id"], 1, first["plan_digest"], fixtures.T0)
        store.transition(
            first["plan_id"], 1, "AWAITING_APPROVAL", fixtures.T1,
            approval_set_id="AS-001",
        )
        store.transition(first["plan_id"], 1, "APPROVED", fixtures.T2)
        with pytest.raises(TransitionNotAllowedError):
            store.transition(
                first["plan_id"], 1, "PUBLISHED", fixtures.T3,
                published_version=99,
            )

    def test_approval_binding_cannot_be_replaced(self):
        store = PlanStore()
        first = content(1)
        store.put_content(first)
        store.create_lifecycle(first["plan_id"], 1, first["plan_digest"], fixtures.T0)
        store.transition(
            first["plan_id"], 1, "AWAITING_APPROVAL", fixtures.T1,
            approval_set_id="AS-001",
        )
        with pytest.raises(TransitionNotAllowedError):
            store.transition(
                first["plan_id"], 1, "AWAITING_APPROVAL", fixtures.T2,
                approval_set_id="AS-OTHER",
            )

    def test_published_can_only_move_to_superseded(self):
        store = PlanStore()
        first = content(1)
        store.put_content(first)
        store.create_lifecycle(first["plan_id"], 1, first["plan_digest"], fixtures.T0)
        store.transition(
            first["plan_id"], 1, "AWAITING_APPROVAL", fixtures.T1,
            approval_set_id="AS-001",
        )
        store.transition(first["plan_id"], 1, "APPROVED", fixtures.T2)
        store.transition(first["plan_id"], 1, "PUBLISHED", fixtures.T3, published_version=1)
        with pytest.raises(TransitionNotAllowedError):
            store.transition(first["plan_id"], 1, "DRAFT", fixtures.T3)
        retired = store.supersede(
            first["plan_id"], 1, "2026-09-16T08:00:00+08:00"
        )
        assert retired["status"] == "SUPERSEDED"
        assert retired["published_version"] == 1

    def test_regeneration_atomically_supersedes_a_published_version(self, tmp_path):
        store, first = store_at_status("PUBLISHED")
        store.commit_new_version(content(2, 0.6), "2026-09-16T08:00:00+08:00")
        assert store.get_lifecycle(first["plan_id"], 1)["status"] == "SUPERSEDED"
        assert store.get_lifecycle(first["plan_id"], 2)["status"] == "DRAFT"
        assert len(store.pending_superseded()) == 1
        path, _ = raw_dump(store, tmp_path)
        restored = PlanStore()
        restored.load_state(path)
        assert restored.pending_superseded() == store.pending_superseded()
