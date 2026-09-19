"""Single validated execution and serialization boundary for public tools."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping
import time
import uuid

from planpilot.validation import validate_tool_payload

from .correlation import ExecutionContext, mint_uuid4, valid_uuid4
from .errors import FrameworkDomainError, internal_error, validated_failure
from .registry import resolve_tool
from .serialization import json_wire
from .transaction import NoOpPreparedCall, PreparedToolCall


_STAGE_BY_TOOL = {
    "load_factory_state": "load_and_input_validation",
    "validate_factory_state": "load_and_input_validation",
    "generate_plan_options": "plan_generation",
    "validate_plan": "independent_plan_validation",
    "request_approval": "serialization_and_response",
    "check_approval_status": "serialization_and_response",
    "publish_plan": "serialization_and_response",
    "log_security_event": "serialization_and_response",
}


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class DeadlineContext:
    stage: str
    budget_seconds: float
    started_at: float
    profiles_completed: tuple[str, ...] = ()

    def error(self, clock: Callable[[], float]) -> FrameworkDomainError | None:
        elapsed = max(0.0, clock() - self.started_at)
        if elapsed <= self.budget_seconds:
            return None
        details = {"stage": self.stage, "elapsed_seconds": elapsed, "budget_seconds": self.budget_seconds}
        if self.stage == "plan_generation":
            details["profiles_completed"] = list(self.profiles_completed)
        return FrameworkDomainError("DEADLINE_EXCEEDED", "The tool stage exceeded its declared deadline.", details)


@dataclass(frozen=True)
class ToolCallContext:
    correlation_id: str
    deadline: DeadlineContext | None
    entity_references: Mapping[str, str]


@dataclass(frozen=True)
class ToolExecutionOutcome:
    tool_name: str
    correlation_id: str
    is_error: bool
    payload: Mapping[str, Any]
    wire_bytes: bytes


class _RollbackGuard:
    def __init__(self, call: PreparedToolCall):
        self.call = call
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.committed:
            return False
        try:
            self.call.rollback()
        except Exception:
            if exc_type is not None and not issubclass(exc_type, Exception):
                return False
            raise
        return False


class ToolErrorMiddleware:
    def __init__(
        self,
        *,
        uuid_factory: Callable[[], uuid.UUID | str] = uuid.uuid4,
        clock: Callable[[], float] = time.monotonic,
        observer: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        self._uuid_factory = uuid_factory
        self._clock = clock
        self._observer = observer

    def _correlation(self, context: ExecutionContext | None) -> tuple[str, bool]:
        if context is None:
            return mint_uuid4(self._uuid_factory), True
        if valid_uuid4(context.correlation_id):
            return context.correlation_id, True
        return mint_uuid4(self._uuid_factory), False

    def _failure(self, tool_name: str, correlation_id: str, exc: Exception) -> ToolExecutionOutcome:
        payload, wire = validated_failure(tool_name, exc, correlation_id)
        return ToolExecutionOutcome(tool_name, correlation_id, True, _freeze(payload), wire)

    def _finish(
        self,
        outcome: ToolExecutionOutcome,
        committed: bool,
        entity_references: Mapping[str, str],
        started_at: float,
        deadline: DeadlineContext | None,
        trace_metadata: Mapping[str, Any],
    ) -> ToolExecutionOutcome:
        if self._observer is not None:
            bounded_refs = {
                str(key)[:100]: str(value)[:200]
                for key, value in sorted(entity_references.items(), key=lambda item: str(item[0]))[:12]
            }
            deadline_metadata = None if deadline is None else _freeze({
                "stage": deadline.stage,
                "budget_seconds": deadline.budget_seconds,
                "profiles_completed": list(deadline.profiles_completed[:3]),
            })
            event = MappingProxyType({
                "tool_name": outcome.tool_name,
                "correlation_id": outcome.correlation_id,
                "is_error": outcome.is_error,
                "error_code": outcome.payload.get("error_code") if outcome.is_error else None,
                "retryable": outcome.payload.get("retryable") if outcome.is_error else None,
                "elapsed_seconds": max(0.0, self._clock() - started_at),
                "deadline": deadline_metadata,
                "payload_bytes": len(outcome.wire_bytes),
                "committed": committed,
                "entity_references": _freeze(bounded_refs),
                "trace_metadata": _freeze(dict(trace_metadata)),
            })
            try:
                self._observer(event)
            except Exception:
                pass
        return outcome

    def execute(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
        call: PreparedToolCall | Callable[[Mapping[str, Any], ToolCallContext], Mapping[str, Any]],
        *,
        context: ExecutionContext | None = None,
        deadline: DeadlineContext | None = None,
        entity_references: Mapping[str, str] | None = None,
        trace_metadata: Mapping[str, Any] | None = None,
    ) -> ToolExecutionOutcome:
        """Validate, prepare once, serialize, commit, then notify once."""
        resolve_tool(tool_name)
        started_at = self._clock()
        correlation_id, context_valid = self._correlation(context)
        refs = dict(entity_references or {})
        metadata = dict(trace_metadata or {})
        if not context_valid:
            outcome = self._failure(tool_name, correlation_id, internal_error("InvalidCorrelationContext"))
            return self._finish(outcome, False, refs, started_at, deadline, metadata)
        if not isinstance(payload, Mapping):
            payload = {"__invalid_payload__": None}
        try:
            validate_tool_payload(dict(payload), tool_name, "input_schema", f"{tool_name}_input")
        except Exception as exc:
            outcome = self._failure(tool_name, correlation_id, exc)
            return self._finish(outcome, False, refs, started_at, deadline, metadata)

        if deadline is not None:
            expected_stage = _STAGE_BY_TOOL[tool_name]
            if deadline.stage != expected_stage:
                outcome = self._failure(tool_name, correlation_id, internal_error("InvalidDeadlineContext"))
                return self._finish(outcome, False, refs, started_at, deadline, metadata)
            expired = deadline.error(self._clock)
            if expired is not None:
                outcome = self._failure(tool_name, correlation_id, expired)
                return self._finish(outcome, False, refs, started_at, deadline, metadata)

        prepared = call if all(callable(getattr(call, name, None)) for name in ("prepare", "commit", "rollback")) else NoOpPreparedCall(call)
        tool_context = ToolCallContext(correlation_id, deadline, MappingProxyType(refs))
        outcome = None
        committed = False
        try:
            with _RollbackGuard(prepared) as guard:
                try:
                    candidate = prepared.prepare(deepcopy(dict(payload)), tool_context)
                except Exception as exc:
                    outcome = self._failure(tool_name, correlation_id, exc)
                if outcome is None:
                    if deadline is not None:
                        expired = deadline.error(self._clock)
                        if expired is not None:
                            outcome = self._failure(tool_name, correlation_id, expired)
                    if outcome is None:
                        try:
                            candidate_copy = deepcopy(dict(candidate))
                            validate_tool_payload(candidate_copy, tool_name, "output_schema", f"{tool_name}_output")
                            wire = json_wire(candidate_copy)
                        except Exception:
                            outcome = self._failure(tool_name, correlation_id, internal_error("ToolOutputValidationFailure"))
                    if outcome is None:
                        try:
                            prepared.commit()
                            guard.committed = True
                            committed = True
                            outcome = ToolExecutionOutcome(tool_name, correlation_id, False, _freeze(candidate_copy), wire)
                        except Exception:
                            outcome = self._failure(tool_name, correlation_id, internal_error("ToolCommitFailure"))
        except Exception:
            outcome = self._failure(tool_name, correlation_id, internal_error("ToolRollbackFailure"))
        return self._finish(outcome, committed, refs, started_at, deadline, metadata)


__all__ = ["DeadlineContext", "ToolCallContext", "ToolErrorMiddleware", "ToolExecutionOutcome"]
