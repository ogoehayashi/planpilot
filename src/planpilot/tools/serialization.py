"""Exact immutable JSON wire bytes and failure-envelope size enforcement."""
from __future__ import annotations

import json
from typing import Any


MAX_FAILURE_BYTES = 4096


class WireSerializationError(ValueError):
    pass


def json_wire(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WireSerializationError("payload is not canonical JSON") from exc


def failure_wire(value: Any) -> bytes:
    wire = json_wire(value)
    if len(wire) > MAX_FAILURE_BYTES:
        raise WireSerializationError("failure envelope exceeds 4096 UTF-8 bytes")
    return wire


__all__ = ["MAX_FAILURE_BYTES", "WireSerializationError", "failure_wire", "json_wire"]
