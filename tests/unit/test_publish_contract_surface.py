"""G2 Phase 0.3 — pin the publish contract surface against V1.8 bytes.

Tasks.md 0.3: assert directly against the on-disk contract (SHA-256
b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639) that the
publish_plan tool surface the publisher will implement actually says what
design.md claims it says:

- input schema: exactly the five binding fields, required, closed;
  idempotency_key bounds 16..128; plan_digest pattern is a 64-hex digest.
- output schema: four fields, status const PUBLISHED, closed.
- failure schema: $ref #/$defs/tool_error; the registry says
  IDEMPOTENCY_CONFLICT / VALIDATION_FAILED / POLICY_VIOLATION are
  non-retryable and INTERNAL_ERROR is retryable (one retry only after a
  successful dependency health check, per retry_policies text).
- details contracts for IDEMPOTENCY_CONFLICT and POLICY_VIOLATION:
  required fields, closed objects, and the violated_policy enum containing
  approval_scope_exceeded (Case D).

If any of these fail, the contract changed under us — the design must be
re-reviewed before Phase 2, not patched around here.
"""

from __future__ import annotations

import hashlib

import _fixtures as fixtures

# tasks.md 0.3: "asserted directly against V1.8 bytes".
CONTRACT_SHA256 = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"


def _contract_bytes() -> bytes:
    return fixtures.CONTRACT_PATH.read_bytes()


def _contract() -> dict:
    import json

    return json.loads(_contract_bytes().decode("utf-8"))


def _publish_tool() -> dict:
    return next(t for t in _contract()["tools"] if t["name"] == "publish_plan")


# ------------------------------------------------------------------ stability

def test_contract_file_is_the_v1_8_bytes_we_reviewed_against():
    assert hashlib.sha256(_contract_bytes()).hexdigest() == CONTRACT_SHA256


# ---------------------------------------------------------------- input face

def test_publish_input_is_exactly_the_five_binding_fields():
    inp = _publish_tool()["input_schema"]
    assert inp["type"] == "object"
    assert set(inp["required"]) == {
        "plan_id",
        "expected_plan_version",
        "plan_digest",
        "approval_set_id",
        "idempotency_key",
    }
    assert set(inp["properties"]) == set(inp["required"])
    assert inp["additionalProperties"] is False


def test_idempotency_key_bounds_and_digest_pattern():
    props = _publish_tool()["input_schema"]["properties"]
    key = props["idempotency_key"]
    assert key["type"] == "string"
    assert key["minLength"] == 16 and key["maxLength"] == 128
    assert props["plan_digest"]["pattern"] == "^[a-f0-9]{64}$"
    assert props["expected_plan_version"]["minimum"] == 0


# --------------------------------------------------------------- output face

def test_publish_output_shape():
    out = _publish_tool()["output_schema"]
    assert set(out["required"]) == {
        "plan_id",
        "published_version",
        "status",
        "audit_log_id",
    }
    assert set(out["properties"]) == set(out["required"])
    assert out["properties"]["status"] == {"const": "PUBLISHED"}
    assert out["additionalProperties"] is False


def test_publish_failure_schema_is_the_tool_error_def():
    assert _publish_tool()["failure_schema"] == {"$ref": "#/$defs/tool_error"}
    te = _contract()["$defs"]["tool_error"]
    assert set(te["required"]) == {
        "error_code",
        "message",
        "retryable",
        "correlation_id",
        "details",
    }
    assert te["additionalProperties"] is False


# ------------------------------------------------------------ retryability

def test_publish_error_retryability_registry():
    reg = _contract()["tool_execution_contract"]["retryability_registry"]
    # Design depends on these four being pinned exactly.
    assert reg["IDEMPOTENCY_CONFLICT"] is False
    assert reg["VALIDATION_FAILED"] is False
    assert reg["POLICY_VIOLATION"] is False
    assert reg["INTERNAL_ERROR"] is True  # but only under the policy below


def test_internal_error_retry_policy_text_one_retry_after_health_check():
    pol = _contract()["tool_execution_contract"]["retry_policies"]
    text = pol["INTERNAL_ERROR"]
    assert "one retry" in text and "health check" in text


def test_registry_covers_every_tool_error_enum_code():
    codes = _contract()["$defs"]["tool_error"]["properties"]["error_code"]["enum"]
    reg = _contract()["tool_execution_contract"]["retryability_registry"]
    assert set(codes) == set(reg), "enum and registry must move together"
    assert len(codes) == 18


# ------------------------------------------------------------- details defs

def test_idempotency_conflict_details_contract():
    d = _contract()["$defs"]["error_details_idempotency_conflict"]
    assert set(d["required"]) == {
        "idempotency_key",
        "original_plan_id",
        "original_status",
    }
    assert d["additionalProperties"] is False


def test_policy_violation_details_contract_and_case_d_enum():
    d = _contract()["$defs"]["error_details_policy_violation"]
    assert set(d["required"]) == {
        "violated_policy",
        "blocked_action",
        "approval_action",
        "security_event_id",
    }
    assert d["additionalProperties"] is False
    enum = d["properties"]["violated_policy"]["enum"]
    assert "approval_scope_exceeded" in enum  # Case D payload key
    assert "publish_plan" in d["properties"]["approval_action"]["anyOf"][0]["enum"]
