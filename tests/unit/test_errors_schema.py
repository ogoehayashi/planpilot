"""THE GHOST TEST — referenced since the first commit, never written.

`src/planpilot/store/errors.py` line 10 and `.kiro/specs/.../tasks.md` line 50
both cite `tests/unit/test_errors_schema.py` as the guard that proves every
error's `.details` satisfies its contract schema. The file did not exist.

That is not a documentation nit. It is the precise reason audit findings F2 and
F5 shipped: PlanNotFoundError defaulted to lookup_kind="plan" (outside the enum)
and DigestMismatchError built details with str(None) (outside ^[a-f0-9]{64}$),
both producing an unemittable tool_error. A schema-validating test would have
caught both on the first run. Cited-but-absent tests are how the orphan-spec
defect class (eight instances found in the V1.8 contract review) happens.

This module makes the citation real. It validates EVERY StoreError subclass's
.details against the schema its own `code` maps to in
tool_execution_contract.details_schemas, and pins the two mirrored vocabularies
(RETRYABILITY, LOOKUP_KINDS) against the contract so neither can drift.
"""

from __future__ import annotations

import re

import pytest

import _fixtures as fixtures
from planpilot import publisher as P
from planpilot.store import errors as E

CONTRACT = fixtures.contract()
DETAILS_SCHEMAS = CONTRACT["tool_execution_contract"]["details_schemas"]
RETRYABILITY_REGISTRY = CONTRACT["tool_execution_contract"]["retryability_registry"]

_HEX64 = "0" * 64
_HEX64B = "f" * 64


def _def_name_for_code(code: str) -> str:
    """Resolve details_schemas[code]["$ref"] to a bare $defs name."""
    ref = DETAILS_SCHEMAS[code]["$ref"]
    m = re.search(r"#/\$defs/(.+)$", ref)
    assert m, f"unexpected $ref shape for {code}: {ref!r}"
    return m.group(1)


# Every StoreError subclass that carries a contract code, with the minimal valid
# constructor arguments. Each MUST produce details that satisfy its schema — that
# is the whole point of this file.
#
# This list is checked for completeness against the actual class hierarchy by
# TestCoverageOfTheErrorHierarchy below, so adding a StoreError subclass without
# adding it here fails the suite instead of silently escaping validation.
def _schema_violation_inner():
    """A real validation failure, used to construct SchemaViolationError.

    Built by actually violating the contract rather than hand-assembling an error
    object, so this entry exercises the same path production does.
    """
    from planpilot.validation import SchemaValidationError, validation_issues_for
    issues = validation_issues_for(
        {"totally_made_up_field": "x"}, "plan_content", "plan_content", "PLAN-1"
    )
    assert issues, "the payload did not actually violate $defs.plan_content"
    return SchemaValidationError(issues, "plan_content", "PLAN-1")


CONCRETE_ERRORS = [
    (E.CanonicalizationError, ("non-finite float", "plan_content.kpis.x"), {}),
    (E.InvalidContentError, ("PLAN-1", 1), {"json_path": "plan_digest", "reason": "malformed"}),
    (E.SchemaViolationError, (_schema_violation_inner(),), {}),
    (E.DigestMismatchError, ("PLAN-1", 1, _HEX64, _HEX64B), {}),
    (E.VersionConflictError, ("PLAN-1", 3, 5), {}),
    (E.IdempotencyConflictError, ("PLAN-1", 2), {"original_status": "DRAFT"}),
    (E.PlanNotFoundError, ("PLAN-1", 1), {}),
    # G2 publisher: publish variant keeps the same code + details shape.
    # Imported here so the hierarchy walk sees it; see
    # tests/unit/test_publisher_idempotency.py for the live-path tests.
    (P.PublishIdempotencyConflictError,
     ("idem-0123456789abcdef0123456789abcdef", "PLAN-1", "DRAFT"), {}),
]


class TestEveryErrorCarriesSchemaValidDetails:
    """F2/F5 guard: no error may be constructed with unemittable details."""

    @pytest.mark.parametrize("cls,args,kwargs", CONCRETE_ERRORS,
                             ids=lambda v: getattr(v, "__name__", None) or "")
    def test_details_satisfy_the_contract_schema(self, cls, args, kwargs):
        err = cls(*args, **kwargs)
        def_name = _def_name_for_code(err.code)
        # Raises on the first schema violation. A malformed `details` fails HERE,
        # not silently at some downstream emit boundary.
        fixtures.validate(err.details, def_name)

    @pytest.mark.parametrize("cls,args,kwargs", CONCRETE_ERRORS,
                             ids=lambda v: getattr(v, "__name__", None) or "")
    def test_code_is_in_the_retryability_registry(self, cls, args, kwargs):
        err = cls(*args, **kwargs)
        assert err.code in RETRYABILITY_REGISTRY, (
            f"{cls.__name__}.code={err.code!r} is not a registered error code"
        )

    @pytest.mark.parametrize("cls,args,kwargs", CONCRETE_ERRORS,
                             ids=lambda v: getattr(v, "__name__", None) or "")
    def test_code_has_a_details_schema(self, cls, args, kwargs):
        err = cls(*args, **kwargs)
        assert err.code in DETAILS_SCHEMAS, (
            f"{cls.__name__}.code={err.code!r} has no details schema mapping"
        )

    def test_to_error_details_returns_a_copy(self):
        """A caller mutating the returned dict must not corrupt the instance."""
        err = E.VersionConflictError("PLAN-1", 3, 5)
        d = err.to_error_details()
        d["expected_plan_version"] = 999
        assert err.details["expected_plan_version"] == 3

    @pytest.mark.parametrize("cls,args,kwargs", CONCRETE_ERRORS,
                             ids=lambda v: getattr(v, "__name__", None) or "")
    def test_an_extra_details_field_fails_validation(self, cls, args, kwargs):
        """Task 2.2: prove these schema tests CAN fail.

        A test that only ever asserts `is valid` is theatre — it would stay green
        against a schema with additionalProperties: true, or against a validator
        that was silently never wired up. Every $defs.error_details_* entry closes
        its object, so injecting one unknown key must flip each to invalid. If any
        of these passes (i.e. the polluted details stay valid), the schema is not
        actually closed and the positive assertions above mean nothing.

        This is the exact assertion task 2.2 asked for and that the first version
        of this file omitted — the file existed, but this half of the requirement
        did not, which is the same shape as the ghost-test defect (F13).
        """
        err = cls(*args, **kwargs)
        def_name = _def_name_for_code(err.code)
        # precondition: the details are valid as emitted
        assert fixtures.is_valid(err.details, def_name), \
            f"{cls.__name__}.details were invalid before pollution"
        polluted = dict(err.details)
        polluted["__field_that_is_not_in_the_contract"] = "x"
        assert not fixtures.is_valid(polluted, def_name), (
            f"{cls.__name__}: adding an unknown field did NOT fail validation, so "
            f"$defs/{def_name} is not closed (additionalProperties: false) and the "
            f"positive assertions in this class are not proving anything"
        )


class TestLookupKindIsClosed:
    """F2 specifically: lookup_kind must be a contract enum member, always."""

    def test_mirrored_enum_equals_the_contract_enum(self):
        contract_enum = DETAILS_SCHEMAS["STATE_NOT_FOUND"]
        # Resolve the enum from the referenced $def
        def_name = _def_name_for_code("STATE_NOT_FOUND")
        schema = CONTRACT["$defs"][def_name]["properties"]["lookup_kind"]["enum"]
        assert set(E.LOOKUP_KINDS) == set(schema)

    def test_every_lookup_kind_produces_valid_details(self):
        for kind in sorted(E.LOOKUP_KINDS):
            err = E.PlanNotFoundError("PLAN-1", 1, lookup_kind=kind)
            fixtures.validate(err.details, _def_name_for_code("STATE_NOT_FOUND"))
            assert err.details["lookup_kind"] == kind

    def test_default_lookup_kind_is_a_valid_member(self):
        """The first version defaulted to 'plan' — not a member (F2)."""
        err = E.PlanNotFoundError("PLAN-1", 1)  # default
        assert err.details["lookup_kind"] in E.LOOKUP_KINDS
        fixtures.validate(err.details, _def_name_for_code("STATE_NOT_FOUND"))

    def test_an_out_of_enum_lookup_kind_is_refused(self):
        """Constructing with a non-member raises rather than emitting bad details."""
        with pytest.raises(E.InvalidContentError):
            E.PlanNotFoundError("PLAN-1", 1, lookup_kind="plan_content_key")


class TestDigestMismatchRequiresWellFormedDigests:
    """F5 specifically: both sides must be 64-hex, or it is InvalidContentError."""

    def test_two_well_formed_digests_produce_valid_details(self):
        err = E.DigestMismatchError("PLAN-1", 1, _HEX64, _HEX64B)
        fixtures.validate(err.details, _def_name_for_code("PLAN_DIGEST_MISMATCH"))

    @pytest.mark.parametrize("bad", [None, "None", "", "xyz", "A" * 64, "0" * 63, 12345])
    def test_a_malformed_declared_digest_is_not_a_mismatch(self, bad):
        """str(None)=='None' would violate ^[a-f0-9]{64}$ — refused instead (F5)."""
        with pytest.raises(E.InvalidContentError) as exc:
            E.DigestMismatchError("PLAN-1", 1, bad, _HEX64B)
        # The refusal itself must carry schema-valid details
        fixtures.validate(exc.value.details, _def_name_for_code("INVALID_INPUT"))

    def test_a_malformed_recomputed_digest_is_refused(self):
        with pytest.raises(E.InvalidContentError):
            E.DigestMismatchError("PLAN-1", 1, _HEX64, "not-hex")


class TestRetryabilityMirrorsTheContract:
    """The module's RETRYABILITY dict must equal the registry for its codes."""

    def test_every_code_matches_the_registry(self):
        for code, retryable in E.RETRYABILITY.items():
            assert code in RETRYABILITY_REGISTRY, f"{code} not registered"
            assert retryable is RETRYABILITY_REGISTRY[code], (
                f"{code}: module says {retryable}, contract says "
                f"{RETRYABILITY_REGISTRY[code]}"
            )

    def test_every_store_error_code_is_non_retryable(self):
        """All five codes this module raises are non-retryable in the registry."""
        for cls, args, kwargs in CONCRETE_ERRORS:
            err = cls(*args, **kwargs)
            assert err.retryable is RETRYABILITY_REGISTRY[err.code]

    def test_retryable_property_reads_the_mirror(self):
        assert E.PlanNotFoundError("P", 1).retryable is False
        assert E.DigestMismatchError("P", 1, _HEX64, _HEX64B).retryable is False


class TestStoreInvariantErrorsAreNotToolErrors:
    """F-STORE-01: these carry NO code and must not be emittable as tool_error.

    They are caller programming errors (a structural impossibility), not runtime
    situations. If one ever gains a `code` or `details`, it would be rendered as
    a tool_error with an unregistered code — which the contract forbids.
    """

    @pytest.mark.parametrize("cls", [E.TransitionNotAllowedError, E.LifecycleAlreadyExistsError])
    def test_invariant_error_is_not_a_store_error(self, cls):
        assert not issubclass(cls, E.StoreError)
        assert issubclass(cls, E.StoreInvariantError)

    def test_transition_not_allowed_has_no_contract_code(self):
        err = E.TransitionNotAllowedError("PLAN-1", 1, "SUPERSEDED", "PUBLISHED")
        assert not hasattr(err, "details")
        assert not hasattr(err, "code")

    def test_lifecycle_already_exists_has_no_contract_code(self):
        err = E.LifecycleAlreadyExistsError("PLAN-1", 1, "APPROVED")
        assert not hasattr(err, "details")


class TestCoverageOfTheErrorHierarchy:
    """The list above is hardcoded, so it can silently miss a new error class.

    That is exactly how this file came to be a ghost: it was cited as the guard
    that validates error details against the contract, but it did not exist, and
    nothing noticed because nothing enumerated what should be in it. Enumerating
    the hierarchy means a new StoreError subclass FAILS until it is added to
    CONCRETE_ERRORS with arguments that actually construct it.
    """

    @staticmethod
    def _all_store_errors():
        """Every concrete StoreError subclass, transitively."""
        found: set[type] = set()
        queue = [E.StoreError]
        while queue:
            cls = queue.pop()
            for sub in cls.__subclasses__():
                if sub not in found:
                    found.add(sub)
                    queue.append(sub)
        return found

    def test_every_store_error_subclass_is_covered(self):
        covered = {cls for cls, _args, _kw in CONCRETE_ERRORS}
        missing = self._all_store_errors() - covered
        assert not missing, (
            f"these StoreError subclasses are never constructed in this file, so "
            f"their .details are never validated against the contract: "
            f"{sorted(c.__name__ for c in missing)}. Add them to CONCRETE_ERRORS."
        )

    def test_every_covered_class_is_really_a_store_error(self):
        """The mirror check: no stale entry for a class that stopped being one."""
        for cls, _args, _kw in CONCRETE_ERRORS:
            assert issubclass(cls, E.StoreError), f"{cls.__name__} is not a StoreError"

    def test_every_store_error_subclass_declares_a_registered_code(self):
        """A code outside the registry would be an unemittable tool_error."""
        for cls in self._all_store_errors():
            code = getattr(cls, "code", None)
            assert code in RETRYABILITY_REGISTRY, (
                f"{cls.__name__}.code={code!r} is not in "
                f"tool_execution_contract.retryability_registry"
            )

    def test_the_store_raises_one_exception_root(self):
        """SchemaViolationError wraps the validation package's own error.

        Without that, the tool layer would need to special-case two hierarchies.
        """
        from planpilot.validation import SchemaValidationError
        assert issubclass(E.SchemaViolationError, E.StoreError)
        assert issubclass(E.SchemaViolationError, ValueError)
        # and the wrapped error is NOT a StoreError — hence the need to wrap it
        assert not issubclass(SchemaValidationError, E.StoreError)

    def test_schema_violation_details_are_emittable(self):
        from planpilot.validation import SchemaValidationError, ValidationIssue
        inner = SchemaValidationError(
            [ValidationIssue(code="INVALID_VALUE", entity_type="plan_content",
                             message="nope", field="kpis", entity_id="PLAN-1")],
            "plan_content", "PLAN-1",
        )
        err = E.SchemaViolationError(inner)
        fixtures.validate(err.details, "error_details_invalid_input")
        assert err.code == "INVALID_INPUT"
        assert err.retryable is False
