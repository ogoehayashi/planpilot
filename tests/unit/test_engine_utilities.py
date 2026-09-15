import pytest
from planpilot.engine import aggregate_approval_status, escalate, framework_error, overtime_minutes, stability


def test_fallback_shares_deadline_and_is_visible():
    result = escalate(lambda: (_ for _ in ()).throw(RuntimeError("solver")), lambda: {"ok": True})
    assert result.provenance == "HEURISTIC_FALLBACK" and result.attempted_rungs == ("CP-SAT", "HEURISTIC_FALLBACK")


def test_overtime_and_stability_are_recomputed():
    assert overtime_minutes(0, 90, [(0, 60)], [(60, 120)]) == 30
    old = [{"order_id": "O", "operation_no": 1, "start_time": "a", "end_time": "b", "machine_id": "M", "worker_id": "W", "duration_min": 5}]
    assert stability(old, old) == 1.0
    changed = [dict(old[0], worker_id="W2")]
    assert stability(old, changed) == 0.0
    assert stability(old, old + [{"order_id": "N", "operation_no": 1}]) == 0.5


def test_error_envelope_is_bounded():
    error = framework_error("RATE_LIMITED", {"retry_after_seconds": 1}, "corr-1", True)
    assert error["retryable"] is True
    bounded = framework_error("INTERNAL_ERROR", {"message": "x" * 10000}, "corr-1")
    assert len(__import__("json").dumps(bounded).encode()) < 4096


def test_approval_aggregate_precedence_is_fail_closed():
    assert aggregate_approval_status(["APPROVED", "PENDING"]) == "PENDING"
    assert aggregate_approval_status(["EXPIRED", "REJECTED"]) == "REJECTED"
    assert aggregate_approval_status(["APPROVED"], invalidated=True) == "INVALIDATED"
    assert aggregate_approval_status(["APPROVED"]) == "APPROVED"
