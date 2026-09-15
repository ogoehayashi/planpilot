"""Small, deterministic orchestration layer.

The model-facing layer may choose tools, but tools remain the authority for
scheduling, validation, KPI calculation and approval.  This module is usable
without network access and provides the trace consumed by the demo UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


Tool = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolTrace:
    name: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    status: str = "COMPLETED"


@dataclass
class AgentResult:
    intent: dict[str, Any]
    response: str
    traces: list[ToolTrace] = field(default_factory=list)
    approval_required: bool = True
    publish_ready: bool = False
    decision_trace: list[dict[str, Any]] = field(default_factory=list)


class ToolRegistry:
    """Allowlisted deterministic tools. Duplicate names are rejected."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, tool: Tool) -> None:
        if not name or name.startswith("_") or name in self._tools:
            raise ValueError(f"invalid or duplicate tool name: {name!r}")
        self._tools[name] = tool

    def call(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ValueError(f"tool is not allowlisted: {name!r}") from exc
        result = tool(dict(payload))
        if not isinstance(result, dict):
            raise TypeError(f"tool {name!r} must return an object")
        return result


class AgentOrchestrator:
    """Execute the safe planning sequence selected from a parsed intent."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def run(self, intent: dict[str, Any]) -> AgentResult:
        if not isinstance(intent, dict) or not isinstance(intent.get("request"), str):
            raise ValueError("intent must contain a textual request")
        traces: list[ToolTrace] = []
        data = {"request": intent["request"], "constraints": intent.get("constraints", {}), "factory_data": intent.get("factory_data")}
        for name in ("validate_input", "generate_candidates", "validate_candidates", "compare_candidates"):
            output = self.registry.call(name, data)
            traces.append(ToolTrace(name, dict(data), output))
            data = {**data, **output}
        needs_approval = bool(data.get("approval_required", True))
        response = str(data.get("explanation", "Candidate plans generated; human approval is required."))
        trace = [{"step": i + 1, "tool": t.name, "status": t.status} for i, t in enumerate(traces)]
        trace.append({"step": len(trace) + 1, "event": "PENDING", "status": "PENDING"})
        return AgentResult(dict(intent), response, traces, needs_approval, False, trace)
