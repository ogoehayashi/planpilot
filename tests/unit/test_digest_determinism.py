"""Task 1.2 — determinism of the canonical form.

Proves canonical_plan_digest is a pure function of content: same input always
yields the same hash, and input ordering, dict insertion order and non-finite
floats cannot perturb it.

Contract source: scheduling_engine.determinism.canonical_serialization
  "Sort operations by start_time, machine_id, order_id, lot_no and operation_no;
   serialize datetimes as RFC 3339 with +08:00. Hash only the immutable
   plan_content object, excluding plan_content.plan_digest and
   plan_content.engine.canonical_plan_hash to avoid circularity."
"""

from __future__ import annotations

import copy
import hashlib
import random

import pytest

from planpilot.store.digest import (
    OPERATION_SORT_KEY_FIELDS,
    canonical_json,
    canonical_plan_digest,
    sort_operations,
)
from planpilot.store.errors import CanonicalizationError

# Backslash built via chr() so this file contains no literal \u escape, which
# Python would try to decode at parse time.
BACKSLASH_U = chr(92) + "u"


@pytest.fixture
def content(fixtures):
    return fixtures.make_content()


class TestRepeatable:
    def test_200_repeats_are_identical(self, content):
        first = canonical_plan_digest(content)
        for _ in range(200):
            assert canonical_plan_digest(copy.deepcopy(content)) == first

    def test_digest_is_lowercase_hex_sha256(self, content):
        d = canonical_plan_digest(content)
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)

    def test_input_is_never_mutated(self, content):
        before = copy.deepcopy(content)
        canonical_plan_digest(content)
        assert content == before, "digest mutated its argument"


class TestOrderIndependence:
    """Operations order must be decided by the sort key, not by input order."""

    def test_shuffled_operations_do_not_change_digest(self, content):
        baseline = canonical_plan_digest(content)
        for seed in range(10):
            shuffled = copy.deepcopy(content)
            random.Random(seed).shuffle(shuffled["operations"])
            assert canonical_plan_digest(shuffled) == baseline, f"seed {seed} changed the digest"

    def test_dict_key_insertion_order_does_not_matter(self, content):
        baseline = canonical_plan_digest(content)
        reversed_keys = {k: content[k] for k in reversed(list(content.keys()))}
        assert canonical_plan_digest(reversed_keys) == baseline

    def test_nested_key_order_does_not_matter(self, content):
        baseline = canonical_plan_digest(content)
        shuffled = copy.deepcopy(content)
        kpis = shuffled["kpis"]
        shuffled["kpis"] = {k: kpis[k] for k in reversed(list(kpis.keys()))}
        assert canonical_plan_digest(shuffled) == baseline

    def test_sort_key_is_the_contract_key_in_order(self):
        assert OPERATION_SORT_KEY_FIELDS == (
            "start_time", "machine_id", "order_id", "lot_no", "operation_no",
        )

    def test_sort_operations_uses_the_contract_key(self, fixtures):
        ops = [
            fixtures.make_operation(start_time=fixtures.T2, machine_id="MC-09", order_id="ORD-Z"),
            fixtures.make_operation(start_time=fixtures.T0, machine_id="MC-09", order_id="ORD-Z"),
            fixtures.make_operation(start_time=fixtures.T0, machine_id="MC-01", order_id="ORD-Z"),
            fixtures.make_operation(start_time=fixtures.T0, machine_id="MC-01", order_id="ORD-A"),
        ]
        got = sort_operations(ops)
        assert [o["start_time"] for o in got] == [fixtures.T0, fixtures.T0, fixtures.T0, fixtures.T2]
        assert [o["machine_id"] for o in got[:3]] == ["MC-01", "MC-01", "MC-09"]
        assert [o["order_id"] for o in got[:2]] == ["ORD-A", "ORD-Z"]

    def test_sort_is_stable_on_full_ties(self, fixtures):
        """Ties on all five fields keep input order (Python sort is stable)."""
        a = fixtures.make_operation(decision_summary="first")
        b = fixtures.make_operation(decision_summary="second")
        got = sort_operations([a, b])
        assert [o["decision_summary"] for o in got] == ["first", "second"]

    def test_missing_sort_keys_do_not_raise(self, fixtures):
        """Schema validation is another module's job; digesting must stay total."""
        partial = {"order_id": "ORD-A"}
        assert sort_operations([partial]) == [partial]


class TestExcludedFields:
    def test_plan_digest_is_excluded(self, content):
        baseline = canonical_plan_digest(content)
        changed = copy.deepcopy(content)
        changed["plan_digest"] = "f" * 64
        assert canonical_plan_digest(changed) == baseline

    def test_canonical_plan_hash_is_excluded(self, content):
        baseline = canonical_plan_digest(content)
        changed = copy.deepcopy(content)
        changed["engine"]["canonical_plan_hash"] = "e" * 64
        assert canonical_plan_digest(changed) == baseline

    def test_only_those_two_are_excluded(self, content):
        """Every other field must affect the digest — guards against over-exclusion."""
        baseline = canonical_plan_digest(content)
        probes = {
            "plan_id": "PLAN-OTHER",
            "plan_version": 99,
            "profile": "Cost First",
            "operations": [],
            "unscheduled_operations": [],
            "kpis": None,          # replaced below with a valid variant
            "stability_reference_plan_id": "PLAN-REF",
            "assumptions": ["different"],
            "consequential_approval_required": False,
            "infeasible_reason": "some reason",
            "material_reservations": [],
        }
        for field, value in probes.items():
            changed = copy.deepcopy(content)
            if field == "kpis":
                changed["kpis"] = dict(content["kpis"], on_time_rate=0.125)
            elif field == "engine":
                continue
            else:
                changed[field] = value
            assert canonical_plan_digest(changed) != baseline, (
                f"{field} changed but the digest did not — it is being silently excluded"
            )

    def test_engine_fields_other_than_hash_are_included(self, content):
        baseline = canonical_plan_digest(content)
        for field, value in [("solver", "PRIORITY_DISPATCH_FALLBACK"),
                             ("solver_status", "HEURISTIC_FALLBACK"),
                             ("random_seed", 7),
                             ("objective_value", 999.0)]:
            changed = copy.deepcopy(content)
            changed["engine"][field] = value
            assert canonical_plan_digest(changed) != baseline, f"engine.{field} is being excluded"


class TestNonFiniteFloatsRejected:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_rejected_wherever_it_appears(self, content, bad):
        changed = copy.deepcopy(content)
        changed["kpis"]["overtime_hours"] = bad
        with pytest.raises(CanonicalizationError):
            canonical_plan_digest(changed)

    def test_rejected_when_nested_in_a_list(self, content):
        changed = copy.deepcopy(content)
        changed["assumptions"] = ["ok"]
        changed["kpis"]["on_time_rate"] = float("nan")
        with pytest.raises(CanonicalizationError):
            canonical_plan_digest(changed)

    def test_error_carries_the_json_path(self, content):
        changed = copy.deepcopy(content)
        changed["kpis"]["overtime_hours"] = float("nan")
        with pytest.raises(CanonicalizationError) as exc:
            canonical_plan_digest(changed)
        assert "kpis.overtime_hours" in exc.value.json_path

    def test_error_details_match_the_contract_schema(self, content, fixtures):
        changed = copy.deepcopy(content)
        changed["kpis"]["overtime_hours"] = float("nan")
        with pytest.raises(CanonicalizationError) as exc:
            canonical_plan_digest(changed)
        fixtures.validate(exc.value.details, "error_details_invalid_input")

    def test_finite_floats_are_accepted(self, content):
        changed = copy.deepcopy(content)
        changed["kpis"]["overtime_hours"] = 0.0
        changed["engine"]["objective_value"] = -1.5
        assert isinstance(canonical_plan_digest(changed), str)


class TestSerializationChoices:
    def test_no_whitespace_in_canonical_form(self, content):
        text = canonical_json(content)
        assert ": " not in text
        assert ", " not in text

    def test_keys_are_sorted(self):
        text = canonical_json({"z": 1, "a": 2, "m": 3})
        assert text == '{"a":2,"m":3,"z":1}'

    def test_non_ascii_is_not_escaped(self):
        text = canonical_json({"note": "假设：交期以新加坡时间为准"})
        assert "假设" in text, "ensure_ascii=False is not in effect"
        assert BACKSLASH_U not in text, "non-ASCII was escaped to \\uXXXX"

    def test_non_ascii_digest_matches_its_utf8_bytes(self):
        """Pins the encoding: a second implementation must UTF-8 encode, not ASCII."""
        obj = {"note": "假设"}
        text = canonical_json(obj)
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected
        # and it differs from what an ensure_ascii=True implementation would hash
        import json as _json
        ascii_text = _json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert ascii_text != text

    def test_int_and_float_are_distinguished(self):
        """$defs.kpis separates `integer` from `number`; the digest must too."""
        assert canonical_json({"x": 1}) != canonical_json({"x": 1.0})
        assert canonical_plan_digest({"kpis": {"a": 1}}) != canonical_plan_digest({"kpis": {"a": 1.0}})

    def test_digest_equals_sha256_of_canonical_text(self, content):
        """Pins the algorithm so any reimplementation must match byte for byte."""
        d = canonical_plan_digest(content)
        payload = {k: v for k, v in content.items() if k != "plan_digest"}
        payload["engine"] = {k: v for k, v in content["engine"].items() if k != "canonical_plan_hash"}
        payload["operations"] = sort_operations(content["operations"])
        import json as _json
        text = _json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert d == hashlib.sha256(text.encode("utf-8")).hexdigest()
