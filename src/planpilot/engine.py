"""Deterministic generation escalation, calendar KPIs and stability utilities."""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Iterable


@dataclass(frozen=True)
class GenerationResult:
    value: object | None
    provenance: str
    elapsed_seconds: float
    attempted_rungs: tuple[str, ...]


def escalate(primary: Callable[[], object], fallback: Callable[[], object], budget_seconds: float = 40.0,
             primary_seconds: float = 32.0) -> GenerationResult:
    """Run one primary/fallback ladder under one monotonic deadline."""
    if budget_seconds <= 0 or primary_seconds <= 0:
        raise ValueError("generation budgets must be positive")
    started = perf_counter()
    try:
        value = primary()
        return GenerationResult(value, "CP-SAT", perf_counter() - started, ("CP-SAT",))
    except Exception as primary_error:
        elapsed = perf_counter() - started
        if elapsed >= budget_seconds:
            raise TimeoutError("generation escalation exhausted") from primary_error
        try:
            value = fallback()
            return GenerationResult(value, "HEURISTIC_FALLBACK", perf_counter() - started, ("CP-SAT", "HEURISTIC_FALLBACK"))
        except Exception as fallback_error:
            raise TimeoutError("generation escalation exhausted") from fallback_error


def overtime_minutes(start: int, end: int, regular: Iterable[tuple[int, int]], overtime: Iterable[tuple[int, int]]) -> int:
    """Count labour minutes in overtime windows, excluding regular minutes."""
    def overlap(a, b, c, d):
        return max(0, min(b, d) - max(a, c))
    total = 0
    for a, b in overtime:
        total += overlap(start, end, a, b)
        for c, d in regular:
            total -= overlap(max(start, a), min(end, b), c, d)
    return max(0, total)


def stability(reference: Iterable[dict], current: Iterable[dict]) -> float:
    """Union-denominator stability; additions and worker changes count as drift."""
    def key(row):
        return (row.get("order_id"), row.get("lot_no", 1), row.get("operation_no"))
    left, right = {key(r): r for r in reference}, {key(r): r for r in current}
    union = set(left) | set(right)
    if not union:
        return 1.0
    unchanged = sum(k in left and k in right and all(left[k].get(f) == right[k].get(f) for f in ("start_time", "end_time", "machine_id", "worker_id", "lot_quantity", "duration_min")) for k in union)
    return unchanged / len(union)


def framework_error(error_code: str, details: dict, correlation_id: str, retryable: bool = False) -> dict:
    """Create the bounded common error envelope used by tool adapters."""
    import json
    if not isinstance(correlation_id, str) or len(correlation_id) > 64:
        raise ValueError("correlation_id is invalid")
    value = {"error_code": error_code, "retryable": bool(retryable), "correlation_id": correlation_id, "details": details}
    if len(json.dumps(value, ensure_ascii=False).encode()) > 4096:
        return {"error_code": "INTERNAL_ERROR", "retryable": False, "correlation_id": correlation_id, "details": {"message": "error envelope exceeded 4096 bytes"}}
    return value


def aggregate_approval_status(statuses: Iterable[str], invalidated: bool = False) -> str:
    """Apply contract precedence: rejection, expiry, invalidation, approval, pending."""
    values = tuple(statuses)
    if "REJECTED" in values:
        return "REJECTED"
    if "EXPIRED" in values:
        return "EXPIRED"
    if invalidated or "INVALIDATED" in values:
        return "INVALIDATED"
    if values and all(value == "APPROVED" for value in values):
        return "APPROVED"
    return "PENDING"
