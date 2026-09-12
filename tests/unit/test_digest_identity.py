"""Task 1.3 — the two digest identities.

$defs.plan_content.properties.plan_digest.description:
  "SHA-256 of the canonical immutable content excluding only plan_digest and
   engine.canonical_plan_hash; **equals engine.canonical_plan_hash**"

So both must hold:
  canonical_plan_digest(content) == content["plan_digest"]
  canonical_plan_digest(content) == content["engine"]["canonical_plan_hash"]

These tests also pin what does NOT enter the digest: lifecycle state. Including
it would mean every status transition changed the digest and invalidated
approvals bound to that digest — which is why plan_store keeps two records.
"""

from __future__ import annotations

import copy

import pytest

from planpilot.store.digest import assert_digest_consistent, canonical_plan_digest
from planpilot.store.errors import DigestMismatchError


@pytest.fixture
def consistent_content(fixtures):
    """plan_content whose plan_digest and canonical_plan_hash are both correct."""
    draft = fixtures.make_content()
    true_digest = canonical_plan_digest(draft)
    content = fixtures.make_content(digest=true_digest)
    # make_content sets both fields from `digest`, so verify the round trip holds
    assert content["plan_digest"] == true_digest
    assert content["engine"]["canonical_plan_hash"] == true_digest
    return content


class TestBothIdentities:
    def test_digest_equals_plan_digest(self, consistent_content):
        assert canonical_plan_digest(consistent_content) == consistent_content["plan_digest"]

    def test_digest_equals_canonical_plan_hash(self, consistent_content):
        assert (
            canonical_plan_digest(consistent_content)
            == consistent_content["engine"]["canonical_plan_hash"]
        )

    def test_assert_digest_consistent_returns_the_digest(self, consistent_content):
        assert assert_digest_consistent(consistent_content) == consistent_content["plan_digest"]

    def test_the_two_hash_fields_are_equal_to_each_other(self, consistent_content):
        assert (
            consistent_content["plan_digest"]
            == consistent_content["engine"]["canonical_plan_hash"]
        )

    def test_fixture_content_conforms_to_plan_content_schema(self, consistent_content, fixtures):
        fixtures.validate(consistent_content, "plan_content")


class TestMismatchDetected:
    def test_tampered_kpi_is_detected(self, consistent_content):
        """The scenario that matters: a fabricated tool result cannot survive."""
        tampered = copy.deepcopy(consistent_content)
        tampered["kpis"]["on_time_rate"] = 1.0  # looks better than reality
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(tampered)

    def test_tampered_operation_is_detected(self, consistent_content):
        tampered = copy.deepcopy(consistent_content)
        tampered["operations"][0]["start_time"] = "2026-09-14T06:00:00+08:00"
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(tampered)

    def test_removed_operation_is_detected(self, consistent_content):
        """Dropping work to flatter the KPIs must not pass."""
        tampered = copy.deepcopy(consistent_content)
        tampered["operations"] = tampered["operations"][:1]
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(tampered)

    def test_tampered_reservation_is_detected(self, consistent_content):
        tampered = copy.deepcopy(consistent_content)
        tampered["material_reservations"][0]["status"] = "READY"
        tampered["material_reservations"][0]["lines"][0]["reserved_quantity_base_units"] = 99999
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(tampered)

    def test_stale_canonical_plan_hash_is_detected(self, consistent_content):
        """plan_digest right, engine.canonical_plan_hash stale -> still a mismatch."""
        broken = copy.deepcopy(consistent_content)
        broken["engine"]["canonical_plan_hash"] = "a" * 64
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(broken)

    def test_stale_plan_digest_is_detected(self, consistent_content):
        broken = copy.deepcopy(consistent_content)
        broken["plan_digest"] = "b" * 64
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(broken)

    def test_both_hashes_agree_with_each_other_but_not_the_content(self, consistent_content):
        """The subtle case: both fields carry the same WRONG value."""
        broken = copy.deepcopy(consistent_content)
        broken["kpis"]["late_orders"] = 0
        # recompute neither field: they still equal each other, but not the content
        assert broken["plan_digest"] == broken["engine"]["canonical_plan_hash"]
        with pytest.raises(DigestMismatchError):
            assert_digest_consistent(broken)


class TestMismatchErrorShape:
    def test_details_match_the_contract_schema(self, consistent_content, fixtures):
        tampered = copy.deepcopy(consistent_content)
        tampered["kpis"]["on_time_rate"] = 1.0
        with pytest.raises(DigestMismatchError) as exc:
            assert_digest_consistent(tampered)
        fixtures.validate(exc.value.details, "error_details_plan_digest_mismatch")

    def test_code_and_retryability_match_the_registry(self, consistent_content, fixtures):
        tampered = copy.deepcopy(consistent_content)
        tampered["kpis"]["on_time_rate"] = 1.0
        with pytest.raises(DigestMismatchError) as exc:
            assert_digest_consistent(tampered)
        registry = fixtures.contract()["tool_execution_contract"]["retryability_registry"]
        assert exc.value.code in registry
        assert exc.value.retryable is registry[exc.value.code]

    def test_details_carry_both_digests_and_they_differ(self, consistent_content):
        tampered = copy.deepcopy(consistent_content)
        tampered["kpis"]["on_time_rate"] = 1.0
        with pytest.raises(DigestMismatchError) as exc:
            assert_digest_consistent(tampered)
        d = exc.value.details
        assert d["expected_plan_digest"] == consistent_content["plan_digest"]
        assert d["recomputed_plan_digest"] == canonical_plan_digest(tampered)
        assert d["expected_plan_digest"] != d["recomputed_plan_digest"]

    def test_details_is_a_copy_so_callers_cannot_corrupt_it(self, consistent_content):
        tampered = copy.deepcopy(consistent_content)
        tampered["kpis"]["on_time_rate"] = 1.0
        with pytest.raises(DigestMismatchError) as exc:
            assert_digest_consistent(tampered)
        got = exc.value.to_error_details()
        got["expected_plan_digest"] = "corrupted"
        assert exc.value.details["expected_plan_digest"] != "corrupted"


class TestLifecycleNeverEntersDigest:
    def test_lifecycle_fields_are_not_in_content(self, consistent_content):
        """plan_content has additionalProperties: false, so lifecycle cannot ride along."""
        for field in ("status", "approval_set_id", "published_version", "updated_at"):
            assert field not in consistent_content

    def test_adding_a_lifecycle_field_makes_content_invalid(self, consistent_content, fixtures):
        polluted = copy.deepcopy(consistent_content)
        polluted["status"] = "PUBLISHED"
        assert not fixtures.is_valid(polluted, "plan_content")

    def test_lifecycle_mutation_leaves_the_content_digest_alone(self, fixtures, consistent_content):
        """Mutating the lifecycle record cannot change the content digest."""
        digest_before = canonical_plan_digest(consistent_content)
        lc = fixtures.make_lifecycle(
            plan_id=consistent_content["plan_id"],
            plan_version=consistent_content["plan_version"],
            plan_digest=digest_before,
            status="DRAFT",
        )
        for status in ("PROPOSED", "AWAITING_APPROVAL", "APPROVED", "PUBLISHED", "SUPERSEDED"):
            lc["status"] = status
            lc["approval_set_id"] = f"AS-{status}"
            lc["published_version"] = consistent_content["plan_version"]
            lc["updated_at"] = fixtures.T3
            fixtures.validate(lc, "plan_lifecycle")
            # content is untouched, so the digest is unchanged
            assert canonical_plan_digest(consistent_content) == digest_before

    def test_aggregate_plan_validates(self, fixtures, consistent_content):
        plan = {
            "content": consistent_content,
            "lifecycle": fixtures.make_lifecycle(
                plan_id=consistent_content["plan_id"],
                plan_version=consistent_content["plan_version"],
                plan_digest=consistent_content["plan_digest"],
            ),
        }
        fixtures.validate(plan, "plan")
