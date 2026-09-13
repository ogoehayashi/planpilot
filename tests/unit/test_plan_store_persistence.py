"""Task 3.3 — the retention promise.

plan_store.retention:
  "Every version and its audit trail is retained for the hackathon evidence pack;
   **digests are recomputable from stored content at any time**."

That sentence is only true if a dumped store can be reloaded and re-digested to
the same values. These tests execute it rather than asserting it in prose.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from planpilot.store import PlanStore, canonical_plan_digest
from planpilot.store.errors import DigestMismatchError


@pytest.fixture
def populated(fixtures):
    """A store with two versions, one superseded, lifecycle records present.

    v2 is added with commit_new_version(), not put_content() — the latter now
    refuses to add a version to an existing plan (audit finding P1-b), because
    only commit_new_version() also retires the previous one. Passing ts=T3 keeps
    the observable state this fixture always had: v1 SUPERSEDED at T3 still bound
    to AS-001, one invalidation event, v2 DRAFT.
    """
    v1_draft = fixtures.make_content(plan_version=1)
    v1 = fixtures.make_content(plan_version=1, digest=canonical_plan_digest(v1_draft))

    v2_draft = fixtures.make_content(plan_version=2, kpis=fixtures.make_kpis(on_time_rate=0.6))
    v2 = fixtures.make_content(
        plan_version=2,
        digest=canonical_plan_digest(v2_draft),
        kpis=fixtures.make_kpis(on_time_rate=0.6),
    )

    s = PlanStore()
    s.put_content(v1)
    s.create_lifecycle(v1["plan_id"], 1, v1["plan_digest"], ts=fixtures.T0)
    s.transition(v1["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                 approval_set_id="AS-001")
    s.commit_new_version(v2, ts=fixtures.T3)
    return s, v1, v2


class TestRoundTrip:
    def test_dump_then_load_preserves_every_version(self, populated, tmp_path):
        store, v1, v2 = populated
        path = tmp_path / "state.json"
        store.dump_state(path)

        restored = PlanStore()
        restored.load_state(path)

        assert restored.versions(v1["plan_id"]) == [1, 2]
        assert len(restored) == 2
        assert restored.get_content(v1["plan_id"], 1) == v1
        assert restored.get_content(v1["plan_id"], 2) == v2

    def test_digests_recompute_identically_after_reload(self, populated, tmp_path):
        """The retention promise, executed."""
        store, v1, v2 = populated
        path = tmp_path / "state.json"
        before = {v: store.verify_digest(v1["plan_id"], v) for v in (1, 2)}
        store.dump_state(path)

        restored = PlanStore()
        restored.load_state(path)
        after = {v: restored.verify_digest(v1["plan_id"], v) for v in (1, 2)}

        assert before == after
        assert before[1] == v1["plan_digest"]
        assert before[2] == v2["plan_digest"]

    def test_lifecycle_state_survives_the_round_trip(self, populated, tmp_path):
        store, v1, _ = populated
        path = tmp_path / "state.json"
        store.dump_state(path)

        restored = PlanStore()
        restored.load_state(path)

        lc1 = restored.get_lifecycle(v1["plan_id"], 1)
        assert lc1["status"] == "SUPERSEDED"
        assert lc1["approval_set_id"] == "AS-001"
        assert lc1["updated_at"] == "2026-09-15T08:00:00+08:00"

    def test_dumping_twice_is_byte_identical(self, populated, tmp_path):
        store, _, _ = populated
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        store.dump_state(a)
        store.dump_state(b)
        assert a.read_bytes() == b.read_bytes()

    def test_dump_order_does_not_depend_on_insertion_order(self, fixtures, tmp_path):
        """Same logical state, different insertion order -> same bytes.

        This test used to write v2 before v1 for one plan. That construction is
        gone with P1-b: put_content() will not add a version to an existing plan,
        and commit_new_version() requires latest + 1, so a single plan's versions
        can now only be inserted in ascending order.

        The property under test is unchanged, so it is exercised the way that is
        still reachable: two plans inserted in opposite order. dump_state() sorts
        by (plan_id, plan_version), so interleaving differently must not change a
        byte — and with two plans this now also covers ordering ACROSS plans,
        which the single-plan version could not.
        """
        plans = []
        for suffix, rate in (("001", 0.6), ("002", 0.7)):
            v1 = fixtures.make_content(plan_version=1, plan_id=f"PLAN-2026-09-14-{suffix}")
            v1 = fixtures.make_content(
                plan_version=1, plan_id=f"PLAN-2026-09-14-{suffix}",
                digest=canonical_plan_digest(v1),
            )
            d2 = fixtures.make_content(
                plan_version=2, plan_id=f"PLAN-2026-09-14-{suffix}",
                kpis=fixtures.make_kpis(on_time_rate=rate),
            )
            v2 = fixtures.make_content(
                plan_version=2, plan_id=f"PLAN-2026-09-14-{suffix}",
                kpis=fixtures.make_kpis(on_time_rate=rate),
                digest=canonical_plan_digest(d2),
            )
            plans.append((v1, v2))

        def build(order):
            s = PlanStore()
            for v1, v2 in order:
                s.put_content(v1)
                s.create_lifecycle(v1["plan_id"], 1, v1["plan_digest"],
                                   ts="2026-09-14T08:00:00+08:00")
                s.transition(v1["plan_id"], 1, "AWAITING_APPROVAL",
                             ts="2026-09-14T10:30:00+08:00", approval_set_id="AS-001")
                s.commit_new_version(v2, ts="2026-09-15T08:00:00+08:00")
            return s

        a = tmp_path / "a.json"
        build(plans).dump_state(a)

        reordered = build(list(reversed(plans)))    # PLAN-...-002 first this time
        b = tmp_path / "b.json"
        reordered.dump_state(b)

        assert len(reordered) == 4, "both stores hold two plans x two versions"
        assert a.read_bytes() == b.read_bytes()


class TestDumpFormat:
    def test_dump_is_lf_only(self, populated, tmp_path):
        """Same rule .gitattributes enforces on the contract: LF is identity-bearing."""
        store, _, _ = populated
        path = tmp_path / "state.json"
        store.dump_state(path)
        raw = path.read_bytes()
        assert raw.count(b"\r\n") == 0
        assert raw.count(b"\r") == 0

    def test_dump_is_canonical_json(self, populated, tmp_path):
        store, _, _ = populated
        path = tmp_path / "state.json"
        store.dump_state(path)
        raw = path.read_bytes().decode("utf-8")
        body = raw.rstrip("\n")
        assert ": " not in body and ", " not in body
        parsed = json.loads(body)
        assert list(parsed) == sorted(parsed), "top-level keys are sorted"

    def test_dump_round_trips_through_json(self, populated, tmp_path):
        store, _, _ = populated
        path = tmp_path / "state.json"
        store.dump_state(path)
        state = json.loads(path.read_bytes().decode("utf-8"))
        assert set(state) == {"content", "lifecycle", "superseded_events", "superseded_acks"}
        assert len(state["content"]) == 2
        assert len(state["lifecycle"]) == 2
        assert len(state["superseded_events"]) == 1
        assert state["superseded_acks"] == []

    def test_non_ascii_survives_unescaped(self, fixtures, tmp_path):
        draft = fixtures.make_content(assumptions=["假设：交期以新加坡时间为准"])
        content = fixtures.make_content(
            digest=canonical_plan_digest(draft), assumptions=["假设：交期以新加坡时间为准"]
        )
        s = PlanStore()
        s.put_content(content)
        path = tmp_path / "state.json"
        s.dump_state(path)
        raw = path.read_bytes().decode("utf-8")
        assert "假设" in raw
        assert (chr(92) + "u") not in raw

        restored = PlanStore()
        restored.load_state(path)
        assert restored.get_content(content["plan_id"], 1)["assumptions"] == ["假设：交期以新加坡时间为准"]


class TestLoadRefusesCorruption:
    def test_a_corrupted_dump_is_refused(self, populated, tmp_path):
        """Loading must not resurrect a tampered plan into the store of record."""
        store, v1, _ = populated
        path = tmp_path / "state.json"
        store.dump_state(path)

        state = json.loads(path.read_bytes().decode("utf-8"))
        state["content"][0]["kpis"]["on_time_rate"] = 1.0   # flatter the KPI
        path.write_bytes(json.dumps(state).encode("utf-8"))

        restored = PlanStore()
        with pytest.raises(DigestMismatchError):
            restored.load_state(path)

    def test_a_refused_load_leaves_the_store_empty(self, populated, tmp_path):
        """Verify-before-insert: a bad dump must not half-load."""
        store, v1, _ = populated
        path = tmp_path / "state.json"
        store.dump_state(path)

        state = json.loads(path.read_bytes().decode("utf-8"))
        state["content"][0]["operations"] = []
        path.write_bytes(json.dumps(state).encode("utf-8"))

        restored = PlanStore()
        with pytest.raises(DigestMismatchError):
            restored.load_state(path)
        assert len(restored) == 0
        assert not restored.has(v1["plan_id"])

    def test_a_second_record_corrupted_also_refuses_everything(self, populated, tmp_path):
        store, _, v2 = populated
        path = tmp_path / "state.json"
        store.dump_state(path)

        state = json.loads(path.read_bytes().decode("utf-8"))
        target = next(r for r in state["content"] if r["plan_version"] == 2)
        target["engine"]["solver_status"] = "OPTIMAL"
        target["kpis"]["late_orders"] = 0
        path.write_bytes(json.dumps(state).encode("utf-8"))

        restored = PlanStore()
        with pytest.raises(DigestMismatchError):
            restored.load_state(path)
        assert len(restored) == 0

    def test_dump_hash_is_recordable_as_evidence(self, populated, tmp_path):
        """The evidence pack needs a stable hash of the dumped state."""
        store, _, _ = populated
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        store.dump_state(a)
        store.dump_state(b)
        ha = hashlib.sha256(a.read_bytes()).hexdigest()
        hb = hashlib.sha256(b.read_bytes()).hexdigest()
        assert ha == hb
        assert len(ha) == 64
