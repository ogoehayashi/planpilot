"""Framework-owned RFC 4122 UUIDv4 correlation context."""
from __future__ import annotations

from dataclasses import dataclass
import re
import uuid
from typing import Callable


UUID4_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")


def mint_uuid4(factory: Callable[[], uuid.UUID | str] = uuid.uuid4) -> str:
    value = str(factory())
    if not valid_uuid4(value):
        raise ValueError("correlation factory did not return a lowercase RFC 4122 UUIDv4")
    return value


def valid_uuid4(value: object) -> bool:
    if not isinstance(value, str) or UUID4_RE.fullmatch(value) is None:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value


@dataclass(frozen=True)
class ExecutionContext:
    correlation_id: str


__all__ = ["ExecutionContext", "UUID4_RE", "mint_uuid4", "valid_uuid4"]
