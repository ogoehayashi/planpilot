"""Staged tool-call protocol; commit occurs only after middleware validation."""
from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol


class PreparedToolCall(Protocol):
    def prepare(self, payload: Mapping[str, Any], context: Any) -> Mapping[str, Any]: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class NoOpPreparedCall:
    """Adapter for read-only handlers; rollback is idempotent."""

    def __init__(self, handler: Callable[[Mapping[str, Any], Any], Mapping[str, Any]]):
        self._handler = handler
        self._prepared = False
        self._rolled_back = False

    def prepare(self, payload, context):
        if self._prepared:
            raise RuntimeError("tool handler prepare may run only once")
        self._prepared = True
        return self._handler(payload, context)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self._rolled_back = True


__all__ = ["NoOpPreparedCall", "PreparedToolCall"]
