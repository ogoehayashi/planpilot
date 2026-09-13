"""Regression tests for the third independent audit.

Four findings, each reproduced against a clean tree before being believed (see
_audit_scratch/verify_audit3.py) and each pinned here by calling PlanStore or the
validation package directly — never by testing a helper that the production path
does not use.

  P0-bis  create_lifecycle() could mint an authority status  -> test_plan_store_invariants
  P1-a    load_state() did not relate events to lifecycle     -> here
  P1-b    put_content() could add a version without retiring  -> here + invariants
  P2      lexicographic contract selection; unpinned override -> here

Two defects found while fixing the above, also pinned here:
  dump_state() emitted superseded_events in insertion order (F4/F12 class)
  tests/_fixtures.py carried a SECOND schema compiler (see that module)

Every mutation of a defence added by this audit has a matching negative-control
entry, so removing one fails the suite rather than passing silently.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

import _fixtures as fixtures
from planpilot.store import CREATION_STATUS, PlanStore, canonical_plan_digest
from planpilot.store.errors import (
    DigestMismatchError,
    InvalidContentError,
    PlanNotFoundError,
    StoreInvariantError,
    VersionRouteError,
)
from planpilot.validation import ContractIntegrityError
from planpilot.validation.schema import _contract_version


# --------------------------------------------------------------------- fixtures

@pytest.fixture
def store():
    return PlanStore()


@pytest.fixture
def content():
    return resign(fixtures.make_content())


def resign(c: dict) -> dict:
    """Recompute both digest identities after mutating a plan."""
    out = copy.deepcopy(c)
    out.pop("plan_digest", None)
    d = canonical_plan_digest(out)
    out["plan_digest"] = d
    if "engine" in out:
        out["engine"]["canonical_plan_hash"] = d
    return out


def next_version(content: dict, version: int, **overrides) -> dict:
    out = copy.deepcopy(content)
    out["plan_version"] = version
    for key, value in overrides.items():
        out.setdefault("kpis", {})[key] = value
    return resign(out)


# ====================================================== P1-a: event <-> lifecycle
#
# load_state() checked each supersede event against the CONTENT record it named,
# but never against the lifecycle record. So a hand-edited dump could rebind an
# event to a different approval set, duplicate an event, or flip a superseded
# lifecycle back to APPROVED and keep the event. All four were measured ACCEPTED.


class TestLoadStateRelatesEventsToLifecycle:
    def _dump_with_supersede(self, store, content, tmp_path):
        """A real two-version state: v1 APPROVED with AS-001, then superseded."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T2)
        store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                 ts=fixtures.T2)
        path = tmp_path / "state.json"
        store.dump_state(path)          # BEFORE draining: drain empties the list
        return path

    def _rewrite(self, path, mutate):
        raw = json.loads(path.read_text(encoding="utf-8"))
        mutate(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_a_valid_superseded_dump_round_trips(self, store, content, tmp_path):
        """The positive control: the real state must still load."""
        path = self._dump_with_supersede(store, content, tmp_path)
        fresh = PlanStore()
        fresh.load_state(path)
        assert fresh.get_lifecycle(content["plan_id"], 1)["status"] == "SUPERSEDED"
        assert len(fresh.pending_superseded()) == 1

    def test_an_event_rebound_to_another_approval_set_is_refused(self, store, content, tmp_path):
        """The audit's case 1. The approval service would invalidate the wrong set."""
        path = self._dump_with_supersede(store, content, tmp_path)

        def rebind(raw):
            raw["superseded_events"][0]["approval_set_id"] = "AS-OTHER"

        self._rewrite(path, rebind)
        fresh = PlanStore()
        with pytest.raises(InvalidContentError) as exc:
            fresh.load_state(path)
        assert "AS-OTHER" in str(exc.value)
        assert len(fresh) == 0

    def test_a_duplicated_supersede_event_is_refused(self, store, content, tmp_path):
        """The audit's case 2. Immutable history is unique per version."""
        path = self._dump_with_supersede(store, content, tmp_path)

        def duplicate(raw):
            raw["superseded_events"].append(copy.deepcopy(raw["superseded_events"][0]))

        self._rewrite(path, duplicate)
        fresh = PlanStore()
        with pytest.raises(InvalidContentError) as exc:
            fresh.load_state(path)
        assert "second history event" in str(exc.value)
        assert len(fresh) == 0

    def test_a_superseded_lifecycle_flipped_back_is_refused(self, store, content, tmp_path):
        """The audit's case 3: status back to APPROVED, event kept.

        That resurrects authority the event claims was withdrawn. Note this is
        refused by the P1-a check (event present but record not SUPERSEDED) and
        ALSO by the layer-3 stale-authority check, so it is caught twice.
        """
        path = self._dump_with_supersede(store, content, tmp_path)

        def flip(raw):
            raw["lifecycle"][0]["status"] = "APPROVED"

        self._rewrite(path, flip)
        fresh = PlanStore()
        with pytest.raises(InvalidContentError):
            fresh.load_state(path)
        assert len(fresh) == 0

    def test_an_event_whose_lifecycle_record_is_deleted_is_refused(self, store, content, tmp_path):
        """The audit's case 4. An event with nothing to describe is a forgery."""
        path = self._dump_with_supersede(store, content, tmp_path)

        def drop(raw):
            raw["lifecycle"] = [r for r in raw["lifecycle"] if r["plan_version"] != 1]

        self._rewrite(path, drop)
        fresh = PlanStore()
        with pytest.raises(InvalidContentError) as exc:
            fresh.load_state(path)
        assert "no lifecycle record" in str(exc.value)
        assert len(fresh) == 0

    def test_a_superseded_record_with_no_event_is_refused(self, store, content, tmp_path):
        """The converse (check 5): SUPERSEDED with a bound approval set but no
        event means the approvals were never invalidated."""
        path = self._dump_with_supersede(store, content, tmp_path)

        def drop_event(raw):
            raw["superseded_events"] = []

        self._rewrite(path, drop_event)
        fresh = PlanStore()
        with pytest.raises(InvalidContentError) as exc:
            fresh.load_state(path)
        assert "audit history is incomplete" in str(exc.value)
        assert len(fresh) == 0

    def test_a_superseded_record_with_no_approval_set_still_needs_history(
        self, store, content, tmp_path
    ):
        """No approval set means no invalidation side effect, not no audit fact."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        # never bound an approval set
        store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                 ts=fixtures.T1)
        path = tmp_path / "unbound.json"
        store.dump_state(path)

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["lifecycle"][0]["approval_set_id"] is None
        raw["superseded_events"] = []
        path.write_text(json.dumps(raw), encoding="utf-8")

        fresh = PlanStore()
        with pytest.raises(InvalidContentError):
            fresh.load_state(path)

    def test_the_event_checks_report_the_event_not_the_content(self, store, content, tmp_path):
        """entity_type must say what was actually rejected (audit-adjacent fix).

        InvalidContentError used to hardcode "plan_content", so a lifecycle or
        event defect was reported as a content defect — the details would have
        misled whoever read the tool_error.
        """
        path = self._dump_with_supersede(store, content, tmp_path)
        self._rewrite(path, lambda raw: raw["superseded_events"][0].__setitem__(
            "approval_set_id", "AS-OTHER"))
        fresh = PlanStore()
        with pytest.raises(InvalidContentError) as exc:
            fresh.load_state(path)
        assert exc.value.details["rejected_entity_type"] == "superseded_event"
        fixtures.validate(exc.value.details, "error_details_invalid_input")


# ============================================ P1-b: layer 3, the dump-side route
#
# put_content() refusing to add a version only closes the live route. Appending a
# v2 content record to a dump whose v1 is APPROVED loaded cleanly, giving
# active == 2 with v1 still APPROVED. Measured ACCEPTED.


class TestLoadStateRefusesStaleAuthority:
    def _dump_with_v1_approved(self, store, content, tmp_path):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.transition(content["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
        store.transition(content["plan_id"], 1, "APPROVED", ts=fixtures.T2)
        path = tmp_path / "approved.json"
        store.dump_state(path)
        return path

    @pytest.mark.parametrize("status", ["APPROVED", "PUBLISHED"])
    def test_a_stale_version_holding_authority_is_refused(self, store, content, tmp_path, status):
        """The audit's P1-b bypass, via the persistence route."""
        path = self._dump_with_v1_approved(store, content, tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["content"].append(next_version(content, 2, on_time_rate=0.6))
        if status == "PUBLISHED":
            raw["lifecycle"][0]["status"] = "PUBLISHED"
            raw["lifecycle"][0]["published_version"] = 1
        path.write_text(json.dumps(raw), encoding="utf-8")

        fresh = PlanStore()
        with pytest.raises(InvalidContentError) as exc:
            fresh.load_state(path)
        assert "stale" in str(exc.value)
        assert exc.value.details["rejected_entity_type"] == "plan_lifecycle"
        assert len(fresh) == 0

    @pytest.mark.parametrize("status", ["DRAFT", "PROPOSED", "BLOCKED"])
    def test_a_stale_version_without_authority_still_loads(self, store, content, tmp_path, status):
        """Layer 3 must not be stricter than transition().

        Measured, not assumed: an earlier version of this check required
        SUPERSEDED, which refused these states — all of which the live
        API produces, because commit_new_version() skips the supersede when the
        previous version has no lifecycle record and then create_lifecycle() can
        attach one. Refusing them broke dump/load round-trip fidelity AND encoded
        a second copy of the authority policy, free to drift from
        _ACTIVE_ONLY_STATUSES. That is the orphan-spec defect class.
        """
        # Build the state through the live API, exactly as probe_roundtrip.py did.
        store.put_content(content)
        store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                 ts=fixtures.T1)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        if status != "DRAFT":
            store.transition(content["plan_id"], 1, status, ts=fixtures.T2,
                             approval_set_id=None)
        assert store.current_active_version(content["plan_id"]) == 2
        assert store.get_lifecycle(content["plan_id"], 1)["status"] == status

        path = tmp_path / "stale.json"
        store.dump_state(path)
        fresh = PlanStore()
        fresh.load_state(path)                      # must NOT raise
        assert fresh.get_lifecycle(content["plan_id"], 1)["status"] == status
        assert fresh.current_active_version(content["plan_id"]) == 2

    def test_a_refused_stale_authority_load_leaves_the_store_empty(self, store, content, tmp_path):
        """No half-load: the content append must not have been committed."""
        path = self._dump_with_v1_approved(store, content, tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["content"].append(next_version(content, 2, on_time_rate=0.6))
        path.write_text(json.dumps(raw), encoding="utf-8")

        fresh = PlanStore()
        with pytest.raises(InvalidContentError):
            fresh.load_state(path)
        assert fresh.versions(content["plan_id"]) == []
        assert not fresh.has(content["plan_id"])


# ================================================== P1-b: the live write route

class TestPutContentRefusesToAddAVersion:
    def test_a_second_version_is_refused(self, store, content):
        store.put_content(content)
        with pytest.raises(VersionRouteError):
            store.put_content(next_version(content, 2, on_time_rate=0.6))
        assert store.versions(content["plan_id"]) == [1]

    def test_the_error_is_an_invariant_error_not_a_tool_error(self, store, content):
        """No registered contract code describes "wrong method" (F-STORE-01).

        POLICY_VIOLATION's violated_policy is a closed enum whose five members
        are all about materials, safety, untrusted data and publishing;
        PLAN_VERSION_CONFLICT means "you expected a different version", which is
        false here. So this is a caller bug and must crash rather than be
        rendered as a tool_error an LLM could act on.
        """
        store.put_content(content)
        with pytest.raises(VersionRouteError) as exc:
            store.put_content(next_version(content, 2, on_time_rate=0.6))
        assert isinstance(exc.value, StoreInvariantError)
        assert not hasattr(exc.value, "details")

    def test_a_different_plan_is_unaffected(self, store, content):
        """The refusal is per plan_id, not global."""
        store.put_content(content)
        other = resign(fixtures.make_content(plan_id="PLAN-2026-09-14-002"))
        store.put_content(other)
        assert store.versions("PLAN-2026-09-14-002") == [1]
        assert store.versions(content["plan_id"]) == [1]

    def test_commit_new_version_is_the_route_that_works(self, store, content):
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        rec = store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                       ts=fixtures.T1)
        assert rec["plan_version"] == 2
        assert rec["status"] == CREATION_STATUS
        assert store.versions(content["plan_id"]) == [1, 2]

    def test_a_skipped_version_is_still_refused_by_commit(self, store, content):
        """Continuity lives on commit_new_version(), and still works."""
        from planpilot.store.errors import VersionConflictError

        store.put_content(content)
        with pytest.raises(VersionConflictError):
            store.commit_new_version(next_version(content, 7, on_time_rate=0.6),
                                     ts=fixtures.T1)
        assert store.versions(content["plan_id"]) == [1]


# ============================================ incidental: dump ordering defect
#
# Found by rewriting the insertion-order test to cover two plans. superseded_events
# was emitted as list(self._superseded) — insertion order — while content and
# lifecycle were sorted. The evidence pack hashes these bytes.


class TestDumpEventOrderIsCanonical:
    def _two_plans(self, fixtures_):
        out = []
        for suffix, rate in (("001", 0.6), ("002", 0.7)):
            pid = f"PLAN-2026-09-14-{suffix}"
            d1 = fixtures_.make_content(plan_version=1, plan_id=pid)
            v1 = fixtures_.make_content(plan_version=1, plan_id=pid,
                                        digest=canonical_plan_digest(d1))
            d2 = fixtures_.make_content(plan_version=2, plan_id=pid,
                                        kpis=fixtures_.make_kpis(on_time_rate=rate))
            v2 = fixtures_.make_content(plan_version=2, plan_id=pid,
                                        kpis=fixtures_.make_kpis(on_time_rate=rate),
                                        digest=canonical_plan_digest(d2))
            out.append((v1, v2))
        return out

    def _build(self, order):
        s = PlanStore()
        for v1, v2 in order:
            s.put_content(v1)
            s.create_lifecycle(v1["plan_id"], 1, v1["plan_digest"], ts=fixtures.T0)
            s.transition(v1["plan_id"], 1, "AWAITING_APPROVAL", ts=fixtures.T1,
                         approval_set_id="AS-001")
            s.commit_new_version(v2, ts=fixtures.T2)
        return s

    def test_events_are_sorted_so_the_dump_is_order_independent(self, fixtures, tmp_path):
        plans = self._two_plans(fixtures)
        a = tmp_path / "a.json"
        self._build(plans).dump_state(a)
        b = tmp_path / "b.json"
        self._build(list(reversed(plans))).dump_state(b)
        assert a.read_bytes() == b.read_bytes()

    def test_the_sorted_events_are_present_in_canonical_order(self, fixtures, tmp_path):
        plans = self._two_plans(fixtures)
        s = self._build(list(reversed(plans)))       # PLAN-...-002 first
        path = tmp_path / "c.json"
        s.dump_state(path)
        events = json.loads(path.read_text(encoding="utf-8"))["superseded_events"]
        assert [e["plan_id"] for e in events] == sorted(e["plan_id"] for e in events)

    def test_an_event_with_a_null_approval_set_does_not_raise_while_sorting(self, store, content, tmp_path):
        """The sort key must be TOTAL: approval_set_id is string|null, and
        None < "AS-001" raises TypeError in Python 3. This reuses digest's
        _total_key, the F12 fix for exactly that hazard."""
        store.put_content(content)
        store.create_lifecycle(content["plan_id"], 1, content["plan_digest"],
                               ts=fixtures.T0)
        store.commit_new_version(next_version(content, 2, on_time_rate=0.6),
                                 ts=fixtures.T1)     # v1 had no approval set
        path = tmp_path / "null.json"
        store.dump_state(path)                        # must not raise
        events = json.loads(path.read_text(encoding="utf-8"))["superseded_events"]
        assert events[0]["approval_set_id"] is None


# ================================================================ P2: contract
#
# contract_path() took sorted(glob)[-1], which is lexicographic, and honoured
# PLANPILOT_CONTRACT_PATH with no digest check.


class TestContractSelectionIsSemantic:
    def test_lexicographic_sort_picks_the_wrong_contract(self):
        """The measured counterexample, pinned so the fix cannot be reverted."""
        names = [
            "planpilot_agent_contract_v1.2.json",
            "planpilot_agent_contract_v1.9.json",
            "planpilot_agent_contract_v1.10.json",
        ]
        assert sorted(names)[-1] == "planpilot_agent_contract_v1.9.json"
        assert max(names, key=_contract_version) == "planpilot_agent_contract_v1.10.json"

    @pytest.mark.parametrize("name,expected", [
        ("planpilot_agent_contract_v1.8.json", (1, 8)),
        ("planpilot_agent_contract_v1.10.json", (1, 10)),
        ("planpilot_agent_contract_v2.0.json", (2, 0)),
        ("planpilot_agent_contract_v1.8.3.json", (1, 8, 3)),
        ("planpilot_agent_contract_v12.json", (12,)),
    ])
    def test_version_parsing(self, name, expected):
        assert _contract_version(name) == expected

    def test_a_shorter_version_does_not_outrank_a_longer_one(self):
        """v1.9 must not beat v1.10 just because '9' > '1' character-wise."""
        assert _contract_version("planpilot_agent_contract_v1.10.json") > \
               _contract_version("planpilot_agent_contract_v1.9.json")

    @pytest.mark.parametrize("name", [
        "planpilot_agent_contract_vX.json",
        "planpilot_agent_contract_v.json",
        "planpilot_agent_contract_v1..8.json",
        "planpilot_agent_contract_v1.8.json.bak",
    ])
    def test_a_malformed_contract_name_is_refused_not_skipped(self, name):
        """Skipping it would let a mis-named contract sit in contract/ unnoticed
        while an older one keeps being used."""
        with pytest.raises(ContractIntegrityError):
            _contract_version(name)


class TestContractOverrideRequiresADigest:
    def test_a_bare_path_override_is_refused(self, tmp_path, monkeypatch):
        """No PLANPILOT_CONTRACT_SHA256 means the caller cannot say which contract
        it meant, so the override is refused rather than trusted.

        The assertion is deliberately narrow, and the negative control is why. An
        earlier version asserted only `"PLANPILOT_CONTRACT_SHA256" in str(exc)`,
        which BOTH branches of the override check satisfy — the digest-mismatch
        message reads "PLANPILOT_CONTRACT_SHA256 says X, file hashes to Y". So
        removing the missing-digest check left the suite green: control fell
        through to `actual != expected`, where expected is None, and still raised.
        The test passed for the wrong reason, exactly the defect class the escaped
        mutation exists to surface.

        So this pins the phrase only the missing-digest branch emits, and asserts
        the other branch's phrase is absent — proving WHICH defence fired.
        """
        from planpilot.validation import schema

        target = tmp_path / "any.json"
        target.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("PLANPILOT_CONTRACT_PATH", str(target))
        monkeypatch.delenv("PLANPILOT_CONTRACT_SHA256", raising=False)
        monkeypatch.setattr(schema, "_VERIFIED", {})

        with pytest.raises(ContractIntegrityError) as exc:
            schema.contract_path()
        message = str(exc.value)
        assert "requires PLANPILOT_CONTRACT_SHA256" in message, message
        # not merely "some integrity error that happens to mention the variable"
        assert "digest mismatch" not in message, (
            "the missing-digest check did not fire; control fell through to the "
            "digest comparison, so this defence is untested"
        )

    def test_a_tampered_contract_is_refused_even_with_a_digest(self, tmp_path, monkeypatch):
        """The audit's P2 case: point the override at a weakened contract.

        Before the fix this retargeted every validator in the package, and a
        1-character plan_id was accepted. The digest must name the file that is
        actually there, so a weakened copy fails unless the caller also computed
        its hash — at which point they are declaring intent, not being tricked.
        """
        from planpilot.validation import schema

        real = json.loads(schema.contract_path().read_text(encoding="utf-8"))
        real["$defs"]["plan_content"]["properties"]["plan_id"]["minLength"] = 1
        target = tmp_path / "weakened.json"
        target.write_text(json.dumps(real), encoding="utf-8")

        monkeypatch.setenv("PLANPILOT_CONTRACT_PATH", str(target))
        monkeypatch.setenv("PLANPILOT_CONTRACT_SHA256", "0" * 64)
        monkeypatch.setattr(schema, "_VERIFIED", {})

        with pytest.raises(ContractIntegrityError) as exc:
            schema.contract_path()
        assert "digest mismatch" in str(exc.value)

    def test_a_matching_digest_is_honoured(self, tmp_path, monkeypatch):
        """The legitimate use: a synthetic contract whose hash the caller knows."""
        from planpilot.validation import schema

        target = tmp_path / "synthetic.json"
        target.write_text(json.dumps({"$defs": {}}), encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()

        monkeypatch.setenv("PLANPILOT_CONTRACT_PATH", str(target))
        monkeypatch.setenv("PLANPILOT_CONTRACT_SHA256", digest)
        monkeypatch.setattr(schema, "_VERIFIED", {})

        assert schema.contract_path() == target.resolve()

    def test_the_production_contract_matches_the_pinned_hash(self):
        """The value conftest and the evidence pack already rely on."""
        from planpilot import CONTRACT_SHA256
        from planpilot.validation import contract_sha256

        assert contract_sha256() == CONTRACT_SHA256
        assert fixtures.contract_hash() == CONTRACT_SHA256

    def test_drift_between_the_disk_contract_and_the_pin_is_refused(self, tmp_path, monkeypatch):
        """Simulates a swapped contract/ file: hash pin must fire.

        _VERIFIED is cleared so the check actually runs; the cache would otherwise
        short-circuit it and the test would pass without exercising the defence.
        """
        from planpilot.validation import schema

        wrong = tmp_path / "planpilot_agent_contract_v9.9.json"
        wrong.write_text(json.dumps({"$defs": {}}), encoding="utf-8")
        monkeypatch.setattr(schema, "_VERIFIED", {})
        monkeypatch.setattr(schema, "_repo_root", lambda start: tmp_path)
        (tmp_path / "contract").mkdir()
        wrong.rename(tmp_path / "contract" / wrong.name)

        with pytest.raises(ContractIntegrityError) as exc:
            schema.contract_path()
        assert "contract drift" in str(exc.value)

    def test_the_highest_semantic_version_is_chosen_not_the_highest_filename(
        self, tmp_path, monkeypatch
    ):
        """The selection logic itself, with several contracts on disk.

        Unit-testing `_contract_version` in isolation is NOT enough: with only
        v1.8 present, reverting `max(..., key=_contract_version)` to
        `sorted(...)[-1]` selects the same file and the whole suite stays green.
        The negative control needs a test that can tell the two apart, so this one
        builds a contract directory holding v1.2, v1.9 and v1.10 and asserts which
        file is resolved.

        CONTRACT_SHA256 is patched to the digest of the file that SHOULD win, so
        the hash pin passes and the assertion measures selection, not integrity.
        """
        import planpilot
        from planpilot.validation import schema

        (tmp_path / "contract").mkdir()
        (tmp_path / "src" / "planpilot").mkdir(parents=True)   # _repo_root markers
        bodies = {}
        for ver in ("1.2", "1.9", "1.10"):
            name = f"planpilot_agent_contract_v{ver}.json"
            body = json.dumps({"$defs": {}, "marker": ver})
            (tmp_path / "contract" / name).write_text(body, encoding="utf-8")
            bodies[name] = body

        winner = "planpilot_agent_contract_v1.10.json"
        loser = "planpilot_agent_contract_v1.9.json"
        monkeypatch.setattr(schema, "_VERIFIED", {})
        monkeypatch.setattr(schema, "_repo_root", lambda start: tmp_path)
        monkeypatch.setattr(
            planpilot, "CONTRACT_SHA256",
            hashlib.sha256(bodies[winner].encode("utf-8")).hexdigest(),
        )

        chosen = schema.contract_path()
        assert chosen.name == winner
        assert chosen.name != loser, "lexicographic selection would have picked v1.9"

    def test_a_misnamed_contract_in_the_directory_is_refused(self, tmp_path, monkeypatch):
        """A file matching the glob but not the version pattern must not be skipped.

        Skipping it would let a mis-named contract sit in contract/ unnoticed
        while an older, well-named one keeps being used.
        """
        import planpilot
        from planpilot.validation import schema

        (tmp_path / "contract").mkdir()
        (tmp_path / "src" / "planpilot").mkdir(parents=True)
        good = tmp_path / "contract" / "planpilot_agent_contract_v1.8.json"
        good.write_text(json.dumps({"$defs": {}}), encoding="utf-8")
        bad = tmp_path / "contract" / "planpilot_agent_contract_vX.json"
        bad.write_text(json.dumps({"$defs": {}}), encoding="utf-8")

        monkeypatch.setattr(schema, "_VERIFIED", {})
        monkeypatch.setattr(schema, "_repo_root", lambda start: tmp_path)
        monkeypatch.setattr(planpilot, "CONTRACT_SHA256",
                            hashlib.sha256(good.read_bytes()).hexdigest())

        with pytest.raises(ContractIntegrityError) as exc:
            schema.contract_path()
        assert "not the expected name shape" in str(exc.value)


# ============================================= single-compiler guarantee

class TestFixturesUseTheProductionCompiler:
    def test_the_fixture_validator_is_the_production_object(self):
        """Identity, not equality: a second compiler would still compare equal
        on a valid instance while disagreeing on an invalid one."""
        from planpilot.validation import schema

        assert fixtures._validator_for("plan_content") is schema.validator_for("plan_content")

    def test_the_fixtures_no_longer_carry_their_own_compiler(self):
        for gone in ("_VALIDATOR", "_SCOPED", "_find_contract"):
            assert not hasattr(fixtures, gone), f"{gone} is back: one compiler only"

    def test_the_fixtures_resolve_the_same_file_as_production(self):
        from planpilot.validation import contract_path

        assert fixtures.CONTRACT_PATH == contract_path()
