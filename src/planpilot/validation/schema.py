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

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

__all__ = [
    "ContractIntegrityError",
    "SchemaValidationError",
    "ValidationIssue",
    "contract_path",
    "contract_sha256",
    "def_names",
    "tool_names",
    "errors_for",
    "is_valid",
    "is_tool_payload_valid",
    "validation_issues_for",
    "validate",
    "validate_tool_payload",
    "validate_shape",
    "validator_for",
    "validator_for_tool",
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
_TOOL_VALIDATORS: dict[tuple[str, str, str], Draft202012Validator] = {}
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


# planpilot_agent_contract_v1.8.json -> (1, 8). Anchored, and the extension is
# part of the pattern so "…_v1.8.json.bak" cannot be mistaken for a contract.
_CONTRACT_NAME_RE = re.compile(r"^planpilot_agent_contract_v(\d+(?:\.\d+)*)\.json$")

# Paths whose sha256 has already been checked. contract_path() is called on
# every validator_for(), and the contract is 190KB, so hashing it per call would
# dominate validation time. Keyed on the resolved path; a path is immutable
# content-wise for the life of the process, and a swapped file under the same
# path is not a threat this repo models (git tracks it and conftest re-checks).
_VERIFIED: dict[str, str] = {}


class ContractIntegrityError(RuntimeError):
    """The contract this package was told to use is not the one it was built for.

    Raised rather than logged, because every validator in the package is compiled
    from this file: validating against a substituted contract would make every
    downstream check meaningless while still reporting success. That is the same
    failure shape as audit finding P0-1 — a gate that always says yes.

    A plain Exception subclass, not a StoreError: this is a deployment/config
    fault at import-or-first-use time, before any plan exists to report against,
    and it has no tool_error mapping.
    """


def _contract_version(name: str) -> tuple[int, ...]:
    """Parse a contract filename into a comparable version tuple.

    Audit finding P2: the previous implementation took `sorted(matches)[-1]`,
    which is LEXICOGRAPHIC. Measured, not assumed —

        sorted(["…_v1.2.json", "…_v1.9.json", "…_v1.10.json"])[-1]
        -> "…_v1.9.json"

    because "1" < "9" and the comparison never reaches the "10". So the release
    after v1.9 would silently have validated against v1.9, and the suite would
    have stayed green: conftest pins the sha256 of whichever file the FIXTURES
    resolve, and both resolvers would have agreed on the wrong file.

    A file matching the contract glob but not the version pattern is refused
    rather than skipped. Skipping it would let a mis-named contract sit in
    contract/ unnoticed while an older one keeps being used.
    """
    m = _CONTRACT_NAME_RE.match(name)
    if m is None:
        raise ContractIntegrityError(
            f"{name!r} matches the contract glob but not the expected name shape "
            f"planpilot_agent_contract_v<numbers>.json; refusing to guess its version"
        )
    return tuple(int(part) for part in m.group(1).split("."))


def contract_sha256(path: Path | None = None) -> str:
    """The sha256 of the contract file, as hex.

    Public so tools and the evidence pack can record the SAME value the
    validators were compiled from, rather than re-hashing a path they resolved
    themselves.
    """
    p = (path or contract_path()).resolve()
    return hashlib.sha256(p.read_bytes()).hexdigest()


def contract_path() -> Path:
    """The authoritative contract this package validates against.

    Resolved from this file's location so it works from any cwd inside a source
    checkout. Packaging the contract as an installed resource is deliberately
    deferred until deployment packaging exists. Two properties, both added for
    audit finding P2:

    1. Highest SEMANTIC version wins, not highest filename (see
       _contract_version for the lexicographic counterexample).
    2. The resolved file's sha256 must equal planpilot.CONTRACT_SHA256.

    Property 2 is what makes property 1 safe. Picking "the newest contract in the
    directory" is only correct if that file is the one this code was written
    against; without the hash check, dropping any file into contract/ would
    silently retarget every validator in the package. tests/conftest.py already
    pinned the hash, but only for the fixtures' own resolution — the production
    entry point was unpinned, so src/ and tests/ could have disagreed.

    PLANPILOT_CONTRACT_PATH still overrides, for a synthetic contract in tests,
    but now requires PLANPILOT_CONTRACT_SHA256 to name that file's digest. A bare
    path is refused. That is deliberate: the override existed to let a test point
    at a contract it constructed, and requiring its digest proves the caller
    knows which contract it meant. It also closes the accidental case — an
    override left in someone's shell can no longer silently retarget production
    validation, since the digest will not match whatever is at that path.

    A boolean "am I in test mode" flag was considered and rejected: it would
    still let the override point at ANY file, so it gates the mode but not the
    content.
    """
    override = os.environ.get("PLANPILOT_CONTRACT_PATH")
    if override:
        resolved = Path(override).resolve()
        expected = os.environ.get("PLANPILOT_CONTRACT_SHA256")
        if not expected:
            raise ContractIntegrityError(
                f"PLANPILOT_CONTRACT_PATH={override!r} requires "
                f"PLANPILOT_CONTRACT_SHA256 to name that file's sha256; an "
                f"unverified override could retarget every validator in this "
                f"package at an unknown contract"
            )
        with _LOCK:
            known = _VERIFIED.get(str(resolved))
        if known is not None:
            if known != expected:
                raise ContractIntegrityError(
                    f"PLANPILOT_CONTRACT_SHA256 changed for the same path "
                    f"({resolved}): {known[:16]}… vs {expected[:16]}…"
                )
            return resolved
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != expected:
            raise ContractIntegrityError(
                f"contract override digest mismatch for {resolved}: "
                f"PLANPILOT_CONTRACT_SHA256 says {expected}, file hashes to {actual}"
            )
        with _LOCK:
            _VERIFIED[str(resolved)] = actual
        return resolved

    root = _repo_root(Path(__file__).resolve())
    directory = root / "contract"
    matches = list(directory.glob("planpilot_agent_contract_v*.json"))
    if not matches:
        raise FileNotFoundError(f"no contract found in {directory}")
    chosen = max(matches, key=lambda p: _contract_version(p.name))

    key = str(chosen)
    with _LOCK:
        known = _VERIFIED.get(key)
    if known is not None:
        return chosen

    # Imported here rather than at module level: this package is inside
    # planpilot, and planpilot/__init__.py deliberately imports nothing, so a
    # top-level `from planpilot import …` would be a cycle waiting to happen the
    # day that file grows an import.
    from planpilot import CONTRACT_SHA256

    actual = hashlib.sha256(chosen.read_bytes()).hexdigest()
    if actual != CONTRACT_SHA256:
        raise ContractIntegrityError(
            f"contract drift: {chosen.name} hashes to {actual}, but this "
            f"implementation was written against {CONTRACT_SHA256}. Every "
            f"validator in planpilot.validation is compiled from this file, so "
            f"validating against a different revision would report success "
            f"against the wrong rules. Update CONTRACT_SHA256 deliberately, or "
            f"restore the contract."
        )
    with _LOCK:
        _VERIFIED[key] = actual
    return chosen


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


def tool_names() -> list[str]:
    """Names of the eight public tools in contract order."""
    return [tool["name"] for tool in _load_contract(contract_path())["tools"]]


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


def validator_for_tool(
    tool_name: str,
    payload_schema: str,
    path: Path | None = None,
) -> Draft202012Validator:
    """Compile one public tool input/output/failure schema with root `$defs`.

    Tool schemas contain references such as ``#/$defs/approval_set_snapshot``;
    validating only the nested schema loses that resolution scope.  This is the
    production compiler for adapters and internal server boundaries that consume
    an exact tool payload. ``payload_schema`` is restricted to the three schema
    keys the contract defines rather than accepting an arbitrary lookup string.
    """
    if payload_schema not in {"input_schema", "output_schema", "failure_schema"}:
        raise KeyError(f"unsupported tool payload schema {payload_schema!r}")
    p = path or contract_path()
    key = (str(p), tool_name, payload_schema)
    with _LOCK:
        cached = _TOOL_VALIDATORS.get(key)
    if cached is not None:
        return cached

    contract = _load_contract(p)
    matches = [tool for tool in contract["tools"] if tool["name"] == tool_name]
    if len(matches) != 1:
        raise KeyError(f"tool {tool_name!r} occurs {len(matches)} times in {p}")
    if payload_schema not in matches[0]:
        raise KeyError(f"tool {tool_name!r} has no {payload_schema}")

    root = Draft202012Validator(
        {"$defs": contract["$defs"]},
        format_checker=jsonschema.FormatChecker(),
    )
    validator = root.evolve(schema=matches[0][payload_schema])
    with _LOCK:
        _TOOL_VALIDATORS[key] = validator
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


def is_tool_payload_valid(
    instance: Any,
    tool_name: str,
    payload_schema: str,
    path: Path | None = None,
) -> bool:
    """True when a payload satisfies one exact public tool schema."""
    return validator_for_tool(tool_name, payload_schema, path).is_valid(instance)


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


def validate_tool_payload(
    instance: Any,
    tool_name: str,
    payload_schema: str,
    entity_type: str,
    entity_id: str | None = None,
    path: Path | None = None,
) -> None:
    """Raise a contract-shaped INVALID_INPUT error for an invalid tool payload."""
    validator = validator_for_tool(tool_name, payload_schema, path)
    issues = [
        _issue_from_error(error, entity_type, entity_id)
        for error in validator.iter_errors(instance)
    ]
    if not issues:
        return
    issues.sort(key=lambda issue: (issue["code"], issue["field"] or "", issue["message"]))
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
