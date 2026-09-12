"""Production contract schema validation.

This package is the ONE place in `src/` that compiles the contract's `$defs` into
executable validators. It exists because of audit finding P0-1: `PlanStore`
checked id/version presence and digest self-consistency but NEVER validated
content against `$defs.plan_content`, so any schema-invalid plan could be written
as long as the caller re-signed its digest. Digest consistency proves content was
not tampered with after signing; it never proves the content is legal.

Why not reuse `tests/_fixtures.py._validator_for`:

That helper is test scaffolding. A production write path must not depend on a
module under `tests/` — tests are not installed, not part of the package, and
their fixture defaults are deliberately synthetic. Both now call this package;
the fixtures module delegates here rather than carrying a second compiler.

Caching: `Draft202012Validator` construction parses the schema and resolves refs.
The contract is 190KB, so validators are compiled once per `$defs` entry and
memoised. The cache is keyed on `(contract_path, def_name)` and is read-only
after construction — safe to share across threads for validation (jsonschema
validators are immutable once built).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

__all__ = [
    "SchemaValidationError",
    "ValidationIssue",
    "contract_path",
    "def_names",
    "errors_for",
    "is_valid",
    "validation_issues_for",
    "validate",
    "validate_shape",
    "validator_for",
]


# ---------------------------------------------------------------------------
# Errors. Carried in the CONTRACT's shape, not a bespoke one.
#
# `error_details_invalid_input` (a closed $defs entry) requires exactly:
#   field_errors: list[$defs/validation_issue] (maxItems 50)
#   rejected_entity_type: string | null
#   rejected_entity_id:   string | null
# with additionalProperties: false. Anything else here is unemittable as a
# tool_error, which is the exact defect class of audit findings F2 and F5.
# ---------------------------------------------------------------------------

# Every code below is a member of $defs.validation_issue_code (36 values).
# The closed-vocabulary guard scans this file, so a fabricated code fails the
# build rather than reaching production.
_CODE_REQUIRED = "REQUIRED_FIELD_MISSING"
_CODE_TYPE = "INVALID_TYPE"
_CODE_VALUE = "INVALID_VALUE"
_CODE_REFERENCE = "BROKEN_REFERENCE"

_SEVERITY_ERROR = "ERROR"


class ValidationIssue(dict):
    """One `$defs.validation_issue`, as a plain dict subclass for readability.

    Required fields: code, severity, entity_type, entity_id, field, message.
    `field` is null for entity-wide issues, per the contract's own description.
    """

    __slots__ = ()

    def __init__(
        self,
        code: str,
        entity_type: str,
        message: str,
        field: str | None = None,
        entity_id: str | None = None,
        severity: str = _SEVERITY_ERROR,
    ) -> None:
        super().__init__(
            code=code,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            message=message,
        )


class SchemaValidationError(ValueError):
    """Instance does not satisfy a contract `$defs` entry.

    `.code` is the registered tool_error code INVALID_INPUT and `.details`
    satisfies `$defs.error_details_invalid_input`, so this is directly
    emittable — no translation layer required at the tool boundary.

    Raised as a ValueError subclass so callers that only expect ValueError from
    malformed input still catch it.
    """

    code = "INVALID_INPUT"

    def __init__(
        self,
        issues: list[ValidationIssue],
        rejected_entity_type: str,
        rejected_entity_id: str | None = None,
    ) -> None:
        self.issues = issues
        self.rejected_entity_type = rejected_entity_type
        self.rejected_entity_id = rejected_entity_id
        # field_errors is capped at 50 by the schema. Reporting more would make
        # the details themselves invalid, so the surplus is summarised in the
        # message instead of silently dropped.
        capped = issues[:50]
        self.details: dict[str, Any] = {
            "field_errors": [dict(i) for i in capped],
            "rejected_entity_type": rejected_entity_type,
            "rejected_entity_id": rejected_entity_id,
        }
        summary = "; ".join(
            f"{i['field'] or i['entity_type']}: {i['message']}" for i in capped[:5]
        )
        extra = f" (+{len(issues) - len(capped)} more)" if len(issues) > len(capped) else ""
        super().__init__(
            f"{rejected_entity_type} rejected by $defs schema: {summary}{extra}"
        )


# ---------------------------------------------------------------------------
# Compiler + cache
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_VALIDATORS: dict[tuple[str, str], Draft202012Validator] = {}
_CONTRACTS: dict[str, dict] = {}


def _repo_root(start: Path) -> Path:
    """Find the repo root by searching upward for the markers.

    Not `parents[N]`: this off-by-one was already made three times in this repo
    (tests/_fixtures.py, tools/write_evidence_plan_store.py,
    tools/factcheck_impl_handoff.py). Searching for a marker cannot go stale
    when a file moves directory depth.
    """
    for parent in [start, *start.parents]:
        if (parent / "contract").is_dir() and (parent / "src" / "planpilot").is_dir():
            return parent
    raise FileNotFoundError(
        f"no repo root (contract/ + src/planpilot/) at or above {start}"
    )


def contract_path() -> Path:
    """The authoritative contract this package validates against.

    Resolved from this file's location so it works installed, in-repo, and from
    any cwd. The caller may override via PLANPILOT_CONTRACT_PATH for tests that
    need a synthetic contract.
    """
    override = os.environ.get("PLANPILOT_CONTRACT_PATH")
    if override:
        return Path(override).resolve()
    root = _repo_root(Path(__file__).resolve())
    matches = sorted((root / "contract").glob("planpilot_agent_contract_v*.json"))
    if not matches:
        raise FileNotFoundError(f"no contract found in {root / 'contract'}")
    # Highest version wins, so bumping the contract needs no code change here.
    return matches[-1]


def _load_contract(path: Path) -> dict:
    key = str(path)
    with _LOCK:
        if key not in _CONTRACTS:
            _CONTRACTS[key] = json.loads(path.read_bytes().decode("utf-8"))
        return _CONTRACTS[key]


# Cache for module-owned shapes (persistence state, supersede events). These are
# not contract $defs — the contract defines no schema for them — but they still
# deserve real enforcement (date-time formats, closed enums, digest patterns,
# additionalProperties: false) rather than hand-rolled isinstance chains. Keyed by
# the schema dict's id(), which is safe because callers pass module-level
# constants that live for the process lifetime.
_SHAPE_VALIDATORS: dict[int, Draft202012Validator] = {}


def _shape_validator(schema: dict) -> Draft202012Validator:
    cached = _SHAPE_VALIDATORS.get(id(schema))
    if cached is None:
        # No $defs bundle here: shapes are self-contained by design. If one ever
        # needs a contract ref, it should be promoted to a real $defs entry
        # instead of inlining a copy that can drift.
        cached = Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        _SHAPE_VALIDATORS[id(schema)] = cached
    return cached


def def_names() -> list[str]:
    """Every `$defs` entry name in the authoritative contract."""
    return sorted(_load_contract(contract_path())["$defs"])


def validator_for(def_name: str, path: Path | None = None) -> Draft202012Validator:
    """A cached validator scoped to `$defs/<def_name>`.

    Uses `evolve()` rather than passing a schema as the second argument to
    validate()/is_valid()/iter_errors() — that form is deprecated in jsonschema
    and will be removed. Fixed now rather than left to surface later.

    `format_checker` is enabled, so `format: date-time` is actually enforced.
    Without it jsonschema treats `format` as an annotation and silently accepts
    garbage timestamps — which is how `updated_at='not a timestamp at all'` got
    stored in audit finding P0-1.
    """
    p = path or contract_path()
    key = (str(p), def_name)
    with _LOCK:
        cached = _VALIDATORS.get(key)
    if cached is not None:
        return cached

    contract = _load_contract(p)
    defs = contract.get("$defs")
    if defs is None:
        raise KeyError(f"{p} has no $defs")
    if def_name not in defs:
        # Fail loudly rather than validating against an empty schema, which
        # would accept anything.
        raise KeyError(f"$defs/{def_name} is not in {p}")

    # Bundle root $defs so "#/$defs/x" refs resolve from any scoped entry.
    root = Draft202012Validator(
        {"$defs": defs},
        format_checker=jsonschema.FormatChecker(),
    )
    validator = root.evolve(schema={"$ref": f"#/$defs/{def_name}"})
    with _LOCK:
        _VALIDATORS[key] = validator
    return validator


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _issue_from_error(
    error: jsonschema.ValidationError,
    entity_type: str,
    entity_id: str | None,
) -> ValidationIssue:
    """Map one jsonschema error onto a contract `$defs.validation_issue`.

    The mapping is deliberately narrow and only ever emits codes that exist in
    $defs.validation_issue_code:
      required        -> REQUIRED_FIELD_MISSING
      type            -> INVALID_TYPE
      $ref resolution -> BROKEN_REFERENCE
      anything else   -> INVALID_VALUE   (enum, pattern, minimum, format, ...)
    """
    if error.validator == "required":
        code = _CODE_REQUIRED
    elif error.validator == "type":
        code = _CODE_TYPE
    elif error.validator == "$ref":
        code = _CODE_REFERENCE
    else:
        code = _CODE_VALUE

    # The offending field: the last path segment for a record-level defect.
    # None (entity-wide) when the failure is at the root, which the schema
    # explicitly allows for `field`.
    field = str(error.path[-1]) if error.path else None
    return ValidationIssue(
        code=code,
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        message=error.message,
    )


def validation_issues_for(
    instance: Any,
    def_name: str,
    entity_type: str,
    entity_id: str | None = None,
    path: Path | None = None,
) -> list[ValidationIssue]:
    """All schema violations as contract-shaped `$defs.validation_issue` records.

    Sorted for determinism: the same bad instance always yields the same list,
    so a digest or a log line built from it is reproducible.
    """
    validator = validator_for(def_name, path)
    issues = [_issue_from_error(e, entity_type, entity_id) for e in validator.iter_errors(instance)]
    issues.sort(key=lambda i: (i["code"], i["field"] or "", i["message"]))
    return issues


def is_valid(
    instance: Any,
    def_name: str,
    path: Path | None = None,
) -> bool:
    """True if `instance` satisfies `$defs/<def_name>` (formats enforced)."""
    return validator_for(def_name, path).is_valid(instance)


def errors_for(
    instance: Any,
    def_name: str,
    path: Path | None = None,
) -> list[str]:
    """Human-readable messages for every violation. For negative tests and logs."""
    validator = validator_for(def_name, path)
    return sorted(e.message for e in validator.iter_errors(instance))


def validate(
    instance: Any,
    def_name: str,
    entity_type: str,
    entity_id: str | None = None,
    path: Path | None = None,
) -> None:
    """Raise `SchemaValidationError` unless `instance` satisfies `$defs/<def_name>`.

    This is the write-boundary entry point. It collects ALL violations rather
    than stopping at the first, because `error_details_invalid_input` carries a
    list — a caller fixing one field should not have to re-run to find the next.
    """
    issues = validation_issues_for(instance, def_name, entity_type, entity_id, path)
    if issues:
        raise SchemaValidationError(issues, entity_type, entity_id)


def validate_shape(
    instance: Any,
    schema: dict,
    entity_type: str,
    entity_id: str | None = None,
) -> None:
    """Validate against a module-owned JSON Schema (not a contract `$defs` entry).

    For shapes this project owns and the contract does not define — the
    persistence state envelope written by `PlanStore.dump_state`, and the
    supersede events the approval service consumes. Hand-rolled isinstance chains
    are how the P1 finding happened: `load_state` restored `superseded_events`
    with no checks at all, so a hand-edited dump could inject a fabricated
    invalidation for a plan that never existed.

    Same error type and same code mapping as `validate`, so callers have one
    exception to handle either way.
    """
    validator = _shape_validator(schema)
    issues = [
        _issue_from_error(e, entity_type, entity_id)
        for e in validator.iter_errors(instance)
    ]
    if not issues:
        return
    issues.sort(key=lambda i: (i["code"], i["field"] or "", i["message"]))
    raise SchemaValidationError(issues, entity_type, entity_id)
