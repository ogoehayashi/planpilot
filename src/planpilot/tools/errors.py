"""Validated framework errors and trusted exception adapters."""
from __future__ import annotations

from copy import deepcopy
import re

from planpilot.approval.errors import ApprovalError
from planpilot.store.errors import StoreError
from planpilot.validation import SchemaValidationError, validate, validate_tool_payload

from .registry import resolve_error, resolve_tool
from .serialization import WireSerializationError, failure_wire


_UNSAFE_MESSAGE = re.compile(r"(?i)(authorization|bearer\s|api[_-]?key|secret\s*=|password\s*=)")


class MiddlewareInvariantError(RuntimeError):
    """The constant safe fallback could not satisfy the pinned contract."""


def _message(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("framework error message must contain 1 to 500 characters")
    if _UNSAFE_MESSAGE.search(value) or any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
        raise ValueError("framework error message contains unsafe content")
    return value


class FrameworkDomainError(Exception):
    """A deliberate domain failure using one registered contract code."""

    def __init__(self, code: str, message: str, details: dict):
        spec = resolve_error(code)
        safe_message = _message(message)
        copied = deepcopy(details)
        validate(copied, spec.details_def, spec.details_def)
        super().__init__(safe_message)
        self.code = code
        self.message = safe_message
        self._details = copied

    @property
    def retryable(self) -> bool:
        return resolve_error(self.code).retryable

    def to_error_details(self) -> dict:
        return deepcopy(self._details)


def internal_error(diagnostic_class: str = "UnexpectedToolFailure") -> FrameworkDomainError:
    name = diagnostic_class if isinstance(diagnostic_class, str) and diagnostic_class.isidentifier() and len(diagnostic_class) <= 120 else "UnexpectedToolFailure"
    return FrameworkDomainError(
        "INTERNAL_ERROR",
        "The tool failed safely; no business state was committed.",
        {"diagnostic_class": name, "safe_detail": "Inspect internal health and retry only under the registered policy."},
    )


def adapt_exception(exc: Exception) -> FrameworkDomainError:
    """Pass only trusted roots; arbitrary code/details attributes are ignored."""
    if isinstance(exc, FrameworkDomainError):
        return FrameworkDomainError(exc.code, exc.message, exc.to_error_details())
    if isinstance(exc, SchemaValidationError):
        return FrameworkDomainError("INVALID_INPUT", "Tool input failed schema validation.", deepcopy(exc.details))
    if isinstance(exc, StoreError):
        return FrameworkDomainError(exc.code, f"Plan store operation failed ({exc.code}).", exc.to_error_details())
    if isinstance(exc, ApprovalError):
        return FrameworkDomainError(exc.code, f"Approval operation failed ({exc.code}).", exc.to_error_details())
    return internal_error(type(exc).__name__)


def _payload(error: FrameworkDomainError, correlation_id: str) -> dict:
    return {
        "error_code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "correlation_id": correlation_id,
        "details": error.to_error_details(),
    }


def validated_failure(tool_name: str, exc: Exception, correlation_id: str) -> tuple[dict, bytes]:
    """Build once, then replace any invalid/oversize failure with one safe fallback."""
    resolve_tool(tool_name)
    try:
        error = adapt_exception(exc)
        payload = _payload(error, correlation_id)
        validate(payload["details"], resolve_error(error.code).details_def, "tool_error_details")
        validate(payload, "tool_error", "tool_error")
        validate_tool_payload(payload, tool_name, "failure_schema", "tool_error")
        return deepcopy(payload), failure_wire(payload)
    except Exception:
        fallback = _payload(internal_error("ToolMiddlewareFailure"), correlation_id)
        try:
            validate(fallback["details"], resolve_error("INTERNAL_ERROR").details_def, "tool_error_details")
            validate(fallback, "tool_error", "tool_error")
            validate_tool_payload(fallback, tool_name, "failure_schema", "tool_error")
            return deepcopy(fallback), failure_wire(fallback)
        except Exception as fatal:
            raise MiddlewareInvariantError("safe INTERNAL_ERROR fallback violates the contract") from fatal


__all__ = ["FrameworkDomainError", "MiddlewareInvariantError", "adapt_exception", "internal_error", "validated_failure"]
