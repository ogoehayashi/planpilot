"""Contract-derived registry for the eight public tools and eighteen errors."""
from __future__ import annotations

from dataclasses import dataclass
import json
from functools import lru_cache

from planpilot.validation import contract_path


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    retryable: bool
    details_def: str
    retry_policy: str | None


@dataclass(frozen=True)
class ToolSpec:
    name: str


@lru_cache(maxsize=1)
def _contract() -> dict:
    return json.loads(contract_path().read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def tool_names() -> tuple[str, ...]:
    return tuple(tool["name"] for tool in _contract()["tools"])


@lru_cache(maxsize=1)
def error_specs() -> dict[str, ErrorSpec]:
    execution = _contract()["tool_execution_contract"]
    details = execution["details_schemas"]
    retryability = execution["retryability_registry"]
    if set(details) != set(retryability):
        raise RuntimeError("contract error registries disagree")
    policies = execution["retry_policies"]
    return {
        code: ErrorSpec(
            code=code,
            retryable=retryability[code],
            details_def=details[code]["$ref"].rsplit("/", 1)[-1],
            retry_policy=policies.get(code),
        )
        for code in retryability
    }


def resolve_tool(name: str) -> ToolSpec:
    if name not in tool_names():
        raise KeyError(f"unknown public tool {name!r}")
    return ToolSpec(name)


def resolve_error(code: str) -> ErrorSpec:
    try:
        return error_specs()[code]
    except KeyError as exc:
        raise KeyError(f"unknown framework error code {code!r}") from exc


def retry_policy(code: str) -> str | None:
    return resolve_error(code).retry_policy


__all__ = ["ErrorSpec", "ToolSpec", "error_specs", "resolve_error", "resolve_tool", "retry_policy", "tool_names"]
