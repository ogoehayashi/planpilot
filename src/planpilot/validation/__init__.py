"""Contract schema validation — the single production validation entry point.

`src/planpilot/validation/` owns compiling the contract's `$defs` into executable
validators. Nothing else in `src/` may build its own validator, and tests must
call this package rather than carrying a second compiler (audit finding P0-1).

Must not import `planpilot.inference`: schema validation is deterministic and
runs on the write path, so it may never reach for an LLM.
"""

from .schema import (
    ContractIntegrityError,
    SchemaValidationError,
    ValidationIssue,
    contract_path,
    contract_sha256,
    def_names,
    errors_for,
    is_valid,
    validation_issues_for,
    validate,
    validate_shape,
    validator_for,
)

__all__ = [
    "ContractIntegrityError",
    "SchemaValidationError",
    "ValidationIssue",
    "contract_path",
    "contract_sha256",
    "def_names",
    "errors_for",
    "is_valid",
    "validation_issues_for",
    "validate",
    "validate_shape",
    "validator_for",
]
