"""Server-owned approval-set lifecycle for immutable validated plans.

No method reads a clock. Every time is supplied by the server adapter, making
expiry, crash recovery and evidence deterministic. Public tool adapters are a
later spec; this module owns the business state they will call.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from planpilot.store import DigestMismatchError, PlanStore, VersionConflictError, canonical_json
from planpilot.validation import validate, validate_tool_payload

from .errors import (
    ApprovalDecisionConflictError,
    ApprovalExpiredError,
    ApprovalInvariantError,
    ApprovalRejectedError,
    ApprovalRequiredError,
    ApprovalSetIncompleteError,
    ApprovalSetInvalidatedError,
    ApprovalStateNotFoundError,
    ApprovalWindowClosedError,
)
from .policy import (
    APPROVAL_ACTIONS,
    DECISION_REASON_CODES,
    DEFAULT_TTL_SECONDS,
    INVALIDATION_CAUSES,
    MAXIMUM_TTL_SECONDS,
    MINIMUM_TTL_SECONDS,
    REASON_BY_ACTION,
    ROLE_BY_ACTION,
    ordered_actions,
)

_SINGAPORE_OFFSET = timedelta(hours=8)
_STATE_KEYS = frozenset({"format_version", "validated_plans", "approval_sets"})
_VALIDATION_RECORD_KEYS = frozenset(
    {"validation_result", "impact_by_action", "horizon_end", "validated_at"}
)
_SET_RECORD_KEYS = frozenset(
    {
        "approval_set_id",
        "plan_id",
        "plan_version",
        "plan_digest",
        "required_actions",
        "approvals",
        "invalidated",
        "invalidation_cause",
        "superseded_plan_digest",
    }
)


def _parse_time(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise ApprovalInvariantError(f"{field} must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalInvariantError(f"{field} is not RFC 3339 date-time: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != _SINGAPORE_OFFSET:
        raise ApprovalInvariantError(f"{field} must carry the +08:00 Singapore offset")
    return parsed


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone(_SINGAPORE_OFFSET)).isoformat(timespec="seconds")


def _binding(result: dict) -> tuple[str, int, str]:
    return result["plan_id"], result["plan_version"], result["plan_digest"]


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:32]}"


class ApprovalService:
    """In-memory approval state with optional atomic canonical persistence."""

    def __init__(self, plan_store: PlanStore, state_path: str | Path | None = None) -> None:
        self._plan_store = plan_store
        self._state_path = Path(state_path) if state_path is not None else None
        self._validated: dict[tuple[str, int, str], dict] = {}
        self._sets: dict[str, dict] = {}
        self._set_by_binding: dict[tuple[str, int, str], str] = {}
        if self._state_path is not None and self._state_path.exists():
            self.load_state(self._state_path)

    # ------------------------------------------------------- validated plans

    def record_validated_plan(
        self,
        validation_result: dict,
        impact_by_action: dict[str, dict],
        horizon_end: str,
        validated_at: str,
    ) -> dict:
        """Store the deterministic validator output used to derive approvals.

        This is an internal server boundary, not a public tool parameter. The
        caller must be the future validate_plan adapter; the LLM and workbook
        cannot supply requirements or impacts.
        """
        record = self._normalize_validation_record(
            validation_result, impact_by_action, horizon_end, validated_at
        )
        plan_id, version, digest = _binding(record["validation_result"])
        key = (plan_id, version, digest)
        existing = self._validated.get(key)
        if existing is not None:
            if canonical_json(existing) != canonical_json(record):
                raise ApprovalDecisionConflictError(
                    "validated-plan binding was replayed with different evidence"
                )
            return copy.deepcopy(existing)
        self._validated[key] = record
        try:
            self._persist()
        except BaseException:
            del self._validated[key]
            raise
        return copy.deepcopy(record)

    # --------------------------------------------------------- set lifecycle

    def request_approval(
        self,
        plan_id: str,
        plan_version: int,
        plan_digest: str,
        action: str,
        server_now: str,
        expires_at: str | None = None,
    ) -> dict:
        """Create the complete server-derived set, or return it idempotently."""
        if action not in APPROVAL_ACTIONS:
            raise ValueError(f"unknown approval trigger action {action!r}")
        now = _parse_time(server_now, "server_now")
        proposed = _parse_time(expires_at, "expires_at") if expires_at is not None else None

        content = self._plan_store.get_content(plan_id, plan_version)
        recomputed = self._plan_store.verify_digest(plan_id, plan_version)
        if plan_digest != recomputed:
            raise DigestMismatchError(plan_id, plan_version, plan_digest, recomputed)
        active = self._plan_store.current_active_version(plan_id)
        if plan_version != active:
            raise VersionConflictError(plan_id, plan_version, active)

        key = (plan_id, plan_version, plan_digest)
        existing_id = self._set_by_binding.get(key)
        if existing_id is not None:
            candidate = copy.deepcopy(self._sets[existing_id])
            changed = self._refresh_expiry(candidate, now)
            self._assert_complete(candidate)
            if changed:
                self._commit_set(existing_id, candidate)
            return self._snapshot(candidate)

        validation_record = self._validated.get(key)
        if validation_record is None:
            raise ApprovalInvariantError(
                "request_approval requires the latest stored validate_plan result for this binding"
            )
        result = validation_record["validation_result"]
        if result["plan_digest"] != content["plan_digest"]:
            raise DigestMismatchError(plan_id, plan_version, result["plan_digest"], recomputed)

        horizon_guard = _parse_time(validation_record["horizon_end"], "horizon_end") + timedelta(
            hours=24
        )
        lower = now + timedelta(seconds=MINIMUM_TTL_SECONDS)
        upper = min(now + timedelta(seconds=MAXIMUM_TTL_SECONDS), horizon_guard)
        if upper < lower:
            raise ApprovalWindowClosedError(
                _format_time(now), _format_time(horizon_guard), MINIMUM_TTL_SECONDS
            )

        required_actions = [r["action"] for r in result["approval_requirements"]]
        required_actions = ordered_actions(required_actions)
        set_id = _stable_id("APS", plan_id, plan_version, plan_digest)
        approvals = []
        for required_action in required_actions:
            candidate = proposed or (
                now + timedelta(seconds=DEFAULT_TTL_SECONDS[required_action])
            )
            effective = min(max(candidate, lower), upper)
            approval = {
                "approval_request_id": _stable_id("APR", set_id, required_action),
                "approval_set_id": set_id,
                "plan_id": plan_id,
                "plan_version": plan_version,
                "plan_digest": plan_digest,
                "action": required_action,
                "approver_role": ROLE_BY_ACTION[required_action],
                "impact_summary": copy.deepcopy(
                    validation_record["impact_by_action"][required_action]
                ),
                "status": "PENDING",
                "decision_reason": None,
                "decision_comment": None,
                "decided_by": None,
                "decided_at": None,
                "expires_at": _format_time(effective),
            }
            validate(approval, "approval_status", "approval_status", approval["approval_request_id"])
            approvals.append(approval)

        record = {
            "approval_set_id": set_id,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "plan_digest": plan_digest,
            "required_actions": required_actions,
            "approvals": approvals,
            "invalidated": False,
            "invalidation_cause": None,
            "superseded_plan_digest": None,
        }
        self._assert_complete(record)
        self._assert_matches_validation(record, validation_record)
        # Commit only after every child and the complete snapshot validate.
        self._sets[set_id] = record
        self._set_by_binding[key] = set_id
        try:
            self._persist()
        except BaseException:
            del self._sets[set_id]
            del self._set_by_binding[key]
            raise
        return self._snapshot(record)

    def check_approval_status(
        self,
        approval_set_id: str,
        plan_id: str,
        plan_version: int,
        plan_digest: str,
        server_now: str,
    ) -> dict:
        record = self._bound_set(approval_set_id, plan_id, plan_version, plan_digest)
        candidate = copy.deepcopy(record)
        changed = self._refresh_expiry(candidate, _parse_time(server_now, "server_now"))
        self._assert_complete(candidate)
        if changed:
            self._commit_set(approval_set_id, candidate)
        return self._snapshot(candidate)

    def record_decision(
        self,
        approval_request_id: str,
        decision: str,
        actor_role: str,
        decided_by: str,
        decided_at: str,
        decision_reason: str | None = None,
        decision_comment: str | None = None,
    ) -> dict:
        """Record one authenticated UI decision and return the complete snapshot."""
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("decision must be APPROVED or REJECTED")
        if not isinstance(decided_by, str) or not decided_by:
            raise ValueError("decided_by must be a non-empty authenticated identity")
        when = _parse_time(decided_at, "decided_at")
        set_id, index = self._find_request(approval_request_id)
        current = self._sets[set_id]
        if current["invalidated"]:
            raise ApprovalSetInvalidatedError(
                set_id, current["invalidation_cause"], current["superseded_plan_digest"]
            )
        approval = current["approvals"][index]
        if actor_role != approval["approver_role"]:
            raise ApprovalInvariantError(
                f"{actor_role!r} cannot decide {approval['action']}; "
                f"requires {approval['approver_role']!r}"
            )
        if decision == "APPROVED" and decision_reason is not None:
            raise ValueError("APPROVED decisions must have decision_reason=null")
        if decision == "REJECTED" and decision_reason not in DECISION_REASON_CODES:
            raise ValueError("REJECTED decisions require a registered decision reason")

        candidate = copy.deepcopy(current)
        changed = self._refresh_expiry(candidate, when)
        approval = candidate["approvals"][index]
        if approval["status"] == "EXPIRED":
            if changed:
                self._assert_complete(candidate)
                self._commit_set(set_id, candidate)
            raise ApprovalExpiredError(approval)

        desired = {
            "status": decision,
            "decision_reason": decision_reason,
            "decision_comment": decision_comment,
            "decided_by": decided_by,
            "decided_at": _format_time(when),
        }
        if approval["status"] != "PENDING":
            actual = {field: approval[field] for field in desired}
            if actual == desired:
                if changed:
                    self._assert_complete(candidate)
                    self._commit_set(set_id, candidate)
                return self._snapshot(candidate)
            raise ApprovalDecisionConflictError(
                f"approval request {approval_request_id} already has a different decision"
            )

        candidate["approvals"][index].update(desired)
        validate(
            candidate["approvals"][index],
            "approval_status",
            "approval_status",
            approval_request_id,
        )
        self._assert_complete(candidate)
        self._commit_set(set_id, candidate)
        return self._snapshot(candidate)

    def require_approved(
        self,
        approval_set_id: str,
        plan_id: str,
        plan_version: int,
        plan_digest: str,
        server_now: str,
    ) -> dict:
        """Return an approved snapshot or raise the exact publish-precondition error."""
        snapshot = self.check_approval_status(
            approval_set_id, plan_id, plan_version, plan_digest, server_now
        )
        record = self._sets[approval_set_id]
        if snapshot["aggregate_status"] == "REJECTED":
            approval = next(a for a in snapshot["approvals"] if a["status"] == "REJECTED")
            raise ApprovalRejectedError(approval)
        if snapshot["aggregate_status"] == "EXPIRED":
            approval = next(a for a in snapshot["approvals"] if a["status"] == "EXPIRED")
            raise ApprovalExpiredError(approval)
        if record["invalidated"]:
            raise ApprovalSetInvalidatedError(
                approval_set_id,
                record["invalidation_cause"],
                record["superseded_plan_digest"],
            )
        if snapshot["aggregate_status"] != "APPROVED":
            pending = [a for a in snapshot["approvals"] if a["status"] == "PENDING"]
            roles = []
            for action in APPROVAL_ACTIONS:
                for approval in pending:
                    if approval["action"] == action and approval["approver_role"] not in roles:
                        roles.append(approval["approver_role"])
            raise ApprovalRequiredError([a["action"] for a in pending], roles)
        return snapshot

    # --------------------------------------------------------- invalidation

    def invalidate_set(
        self,
        approval_set_id: str,
        invalidation_cause: str,
        superseded_plan_digest: str | None,
    ) -> dict:
        if invalidation_cause not in INVALIDATION_CAUSES:
            raise ValueError(f"unknown invalidation cause {invalidation_cause!r}")
        record = self._sets.get(approval_set_id)
        if record is None:
            raise ApprovalStateNotFoundError(approval_set_id)
        if record["invalidated"]:
            if (
                record["invalidation_cause"] != invalidation_cause
                or record["superseded_plan_digest"] != superseded_plan_digest
            ):
                raise ApprovalDecisionConflictError(
                    f"approval set {approval_set_id} was invalidated with different evidence"
                )
            return self._snapshot(record)
        candidate = copy.deepcopy(record)
        candidate["invalidated"] = True
        candidate["invalidation_cause"] = invalidation_cause
        candidate["superseded_plan_digest"] = superseded_plan_digest
        self._assert_complete(candidate)
        self._commit_set(approval_set_id, candidate)
        return self._snapshot(candidate)

    def consume_superseded(
        self,
        acknowledged_at: str,
        *,
        after_invalidation_commit: Callable[[dict], None] | None = None,
    ) -> int:
        """Consume PlanStore events with durable invalidation-before-ack ordering."""
        _parse_time(acknowledged_at, "acknowledged_at")
        events = self._plan_store.pending_superseded()
        consumed = 0
        for event in events:
            binding = (event["plan_id"], event["plan_version"], event["plan_digest"])
            bound_set_id = self._set_by_binding.get(binding)
            event_set_id = event["approval_set_id"]
            if (
                event_set_id is not None
                and bound_set_id is not None
                and event_set_id != bound_set_id
            ):
                raise ApprovalInvariantError(
                    "supersede event names a different set than the approval binding index"
                )
            # The lifecycle may still be DRAFT when request_approval creates a
            # set. In that case PlanStore truthfully emits approval_set_id=null;
            # the approval service still owns the binding and must invalidate it.
            set_id = event_set_id or bound_set_id
            if set_id is not None:
                if self._state_path is None:
                    raise ApprovalInvariantError(
                        "supersede consumption requires durable approval state_path before ack"
                    )
                record = self._sets.get(set_id)
                if record is None:
                    raise ApprovalStateNotFoundError(set_id)
                if (
                    record["plan_id"] != event["plan_id"]
                    or record["plan_version"] != event["plan_version"]
                    or record["plan_digest"] != event["plan_digest"]
                ):
                    raise ApprovalInvariantError(
                        "supersede event binding does not match its approval set"
                    )
                self.invalidate_set(
                    set_id, event["invalidation_cause"], event["plan_digest"]
                )
                # invalidate_set has atomically persisted. This hook exists only
                # to deterministically reproduce a crash before acknowledgement.
                if after_invalidation_commit is not None:
                    after_invalidation_commit(copy.deepcopy(event))
            self._plan_store.acknowledge_superseded(
                event["plan_id"],
                event["plan_version"],
                event["plan_digest"],
                event_set_id,
                acknowledged_at,
            )
            consumed += 1
        return consumed

    # ------------------------------------------------------------ persistence

    def dump_state(self, path: str | Path | None = None) -> str:
        target = Path(path) if path is not None else self._state_path
        if target is None:
            raise ApprovalInvariantError("dump_state requires a path")
        payload = {
            "format_version": 1,
            "validated_plans": [
                copy.deepcopy(record)
                for _, record in sorted(self._validated.items(), key=lambda item: item[0])
            ],
            "approval_sets": [copy.deepcopy(self._sets[key]) for key in sorted(self._sets)],
        }
        text = canonical_json(payload) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
        return text

    def load_state(self, path: str | Path | None = None) -> None:
        source = Path(path) if path is not None else self._state_path
        if source is None:
            raise ApprovalInvariantError("load_state requires a path")
        raw = json.loads(source.read_bytes().decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != _STATE_KEYS or raw["format_version"] != 1:
            raise ApprovalInvariantError("approval state envelope is not the supported closed shape")
        if not isinstance(raw["validated_plans"], list) or not isinstance(raw["approval_sets"], list):
            raise ApprovalInvariantError("approval state collections must be arrays")

        validated: dict[tuple[str, int, str], dict] = {}
        for record in raw["validated_plans"]:
            normalized = self._validate_loaded_validation_record(record)
            key = _binding(normalized["validation_result"])
            if key in validated:
                raise ApprovalInvariantError("duplicate validated-plan binding in approval state")
            validated[key] = normalized

        sets: dict[str, dict] = {}
        bindings: dict[tuple[str, int, str], str] = {}
        for record in raw["approval_sets"]:
            self._assert_complete(record)
            set_id = record["approval_set_id"]
            key = (record["plan_id"], record["plan_version"], record["plan_digest"])
            if set_id in sets or key in bindings:
                raise ApprovalInvariantError("duplicate approval-set id or binding in approval state")
            if key not in validated:
                raise ApprovalInvariantError("approval set has no matching validated-plan record")
            self._assert_matches_validation(record, validated[key])
            sets[set_id] = copy.deepcopy(record)
            bindings[key] = set_id

        # Replace only after every record and relationship has passed.
        self._validated = validated
        self._sets = sets
        self._set_by_binding = bindings

    # --------------------------------------------------------------- helpers

    def _persist(self) -> None:
        if self._state_path is not None:
            self.dump_state(self._state_path)

    def _commit_set(self, set_id: str, candidate: dict) -> None:
        previous = self._sets[set_id]
        self._sets[set_id] = candidate
        try:
            self._persist()
        except BaseException:
            self._sets[set_id] = previous
            raise

    def _normalize_validation_record(
        self,
        validation_result: dict,
        impact_by_action: dict[str, dict],
        horizon_end: str,
        validated_at: str,
    ) -> dict:
        validate_tool_payload(
            validation_result,
            "validate_plan",
            "output_schema",
            "validate_plan_output",
            str(validation_result.get("plan_id"))
            if isinstance(validation_result, dict)
            else None,
        )
        plan_id, version, digest = _binding(validation_result)
        content = self._plan_store.get_content(plan_id, version)
        recomputed = self._plan_store.verify_digest(plan_id, version)
        if digest != recomputed:
            raise DigestMismatchError(plan_id, version, digest, recomputed)
        if validation_result["recomputed_plan_digest"] != recomputed:
            raise DigestMismatchError(
                plan_id, version, validation_result["recomputed_plan_digest"], recomputed
            )
        if canonical_json(validation_result["kpis"]) != canonical_json(content["kpis"]):
            raise ApprovalInvariantError("validate_plan KPIs disagree with immutable plan content")
        if not validation_result["is_feasible"] or validation_result["hard_violations"]:
            raise ApprovalInvariantError(
                "only a feasible, hard-violation-free plan may enter approval"
            )

        _parse_time(horizon_end, "horizon_end")
        _parse_time(validated_at, "validated_at")
        requirements = validation_result["approval_requirements"]
        actions = [requirement["action"] for requirement in requirements]
        if len(actions) != len(set(actions)):
            raise ApprovalInvariantError("validate_plan returned duplicate approval requirements")
        required_actions = ordered_actions(actions)
        if "publish_plan" not in required_actions:
            raise ApprovalInvariantError("publish_plan confirmation is always required")
        for requirement in requirements:
            validate(requirement, "approval_requirement", "approval_requirement", plan_id)
            if requirement["approver_role"] != ROLE_BY_ACTION[requirement["action"]]:
                raise ApprovalInvariantError(
                    f"wrong approver role for {requirement['action']}: "
                    f"{requirement['approver_role']!r}"
                )

        overtime_required = content["kpis"]["overtime_hours"] > 0
        secondary_required = content["kpis"]["secondary_skill_assignment_count"] > 0
        if ("add_overtime" in required_actions) != overtime_required:
            raise ApprovalInvariantError("add_overtime requirement disagrees with recomputed KPI")
        if ("assign_qualified_secondary_skill" in required_actions) != secondary_required:
            raise ApprovalInvariantError(
                "secondary-skill requirement disagrees with recomputed KPI"
            )

        if not isinstance(impact_by_action, dict) or set(impact_by_action) != set(
            required_actions
        ):
            raise ApprovalInvariantError(
                "impact_by_action must contain exactly the server-derived required actions"
            )
        plan_order_ids = {
            item["order_id"]
            for field in ("operations", "unscheduled_operations")
            for item in content[field]
        }
        normalized_impacts: dict[str, dict] = {}
        for action in required_actions:
            impact = copy.deepcopy(impact_by_action[action])
            validate(impact, "approval_impact", "approval_impact", plan_id)
            if REASON_BY_ACTION[action] not in impact["reason_codes"]:
                raise ApprovalInvariantError(
                    f"impact for {action} lacks reason {REASON_BY_ACTION[action]}"
                )
            if not set(impact["affected_order_ids"]).issubset(plan_order_ids):
                raise ApprovalInvariantError(f"impact for {action} names an order absent from plan")
            if impact["changed_operation_count"] > len(content["operations"]):
                raise ApprovalInvariantError(
                    f"impact for {action} exceeds the plan operation count"
                )
            if not set(impact["kpi_deltas"]).issubset(content["kpis"]):
                raise ApprovalInvariantError(f"impact for {action} names an unknown KPI")
            impact["affected_order_ids"] = sorted(impact["affected_order_ids"])
            impact["reason_codes"] = sorted(impact["reason_codes"])
            normalized_impacts[action] = impact

        normalized_result = copy.deepcopy(validation_result)
        normalized_result["approval_requirements"] = sorted(
            normalized_result["approval_requirements"],
            key=lambda requirement: APPROVAL_ACTIONS.index(requirement["action"]),
        )
        return {
            "validation_result": normalized_result,
            "impact_by_action": normalized_impacts,
            "horizon_end": horizon_end,
            "validated_at": validated_at,
        }

    def _validate_loaded_validation_record(self, record: dict) -> dict:
        if not isinstance(record, dict) or set(record) != _VALIDATION_RECORD_KEYS:
            raise ApprovalInvariantError("validated-plan record has unknown or missing fields")
        normalized = self._normalize_validation_record(
            record["validation_result"],
            record["impact_by_action"],
            record["horizon_end"],
            record["validated_at"],
        )
        if canonical_json(record) != canonical_json(normalized):
            raise ApprovalInvariantError("validated-plan record is not in canonical server form")
        return normalized

    def _assert_complete(self, record: dict) -> None:
        set_id = record.get("approval_set_id", "") if isinstance(record, dict) else ""
        if not isinstance(record, dict) or set(record) != _SET_RECORD_KEYS:
            raise ApprovalSetIncompleteError(str(set_id), [], 1, 0)
        required = record["required_actions"]
        approvals = record["approvals"]
        if not isinstance(required, list) or not isinstance(approvals, list):
            raise ApprovalSetIncompleteError(set_id, [], 1, 0)
        try:
            ordered = ordered_actions(required)
        except ValueError as exc:
            raise ApprovalSetIncompleteError(set_id, [], len(required), len(approvals)) from exc
        actions = [a.get("action") for a in approvals if isinstance(a, dict)]
        missing = [action for action in ordered if action not in actions]
        structurally_complete = (
            required == ordered
            and len(required) == len(set(required))
            and len(approvals) == len(required)
            and len(actions) == len(approvals)
            and set(actions) == set(required)
        )
        if not structurally_complete:
            raise ApprovalSetIncompleteError(
                set_id, missing, max(1, len(required)), len(approvals)
            )
        if not isinstance(record["invalidated"], bool):
            raise ApprovalInvariantError("invalidated must be boolean")
        if record["invalidated"]:
            if record["invalidation_cause"] not in INVALIDATION_CAUSES:
                raise ApprovalInvariantError("invalidated set lacks a registered cause")
            if record["superseded_plan_digest"] is not None and (
                not isinstance(record["superseded_plan_digest"], str)
                or len(record["superseded_plan_digest"]) != 64
                or any(c not in "0123456789abcdef" for c in record["superseded_plan_digest"])
            ):
                raise ApprovalInvariantError("invalidated set carries a malformed superseded digest")
        elif record["invalidation_cause"] is not None or record["superseded_plan_digest"] is not None:
            raise ApprovalInvariantError("live set carries invalidation evidence")

        for approval in approvals:
            validate(
                approval,
                "approval_status",
                "approval_status",
                str(approval.get("approval_request_id")),
            )
            if (
                approval["approval_set_id"] != set_id
                or approval["plan_id"] != record["plan_id"]
                or approval["plan_version"] != record["plan_version"]
                or approval["plan_digest"] != record["plan_digest"]
                or approval["approver_role"] != ROLE_BY_ACTION[approval["action"]]
            ):
                raise ApprovalInvariantError("approval child binding or role is contradictory")
        validate(self._snapshot(record), "approval_set_snapshot", "approval_set_snapshot", set_id)

    def _assert_matches_validation(self, record: dict, validation_record: dict) -> None:
        expected_actions = ordered_actions(
            requirement["action"]
            for requirement in validation_record["validation_result"]["approval_requirements"]
        )
        if record["required_actions"] != expected_actions:
            raise ApprovalInvariantError(
                "approval-set membership disagrees with its validated-plan evidence"
            )
        expected_set_id = _stable_id(
            "APS", record["plan_id"], record["plan_version"], record["plan_digest"]
        )
        if record["approval_set_id"] != expected_set_id:
            raise ApprovalInvariantError("approval_set_id is not deterministic for its binding")
        for approval in record["approvals"]:
            action = approval["action"]
            if approval["approval_request_id"] != _stable_id(
                "APR", expected_set_id, action
            ):
                raise ApprovalInvariantError(
                    "approval_request_id is not deterministic for its binding and action"
                )
            if canonical_json(approval["impact_summary"]) != canonical_json(
                validation_record["impact_by_action"][action]
            ):
                raise ApprovalInvariantError(
                    f"approval impact for {action} disagrees with validated evidence"
                )

    def _snapshot(self, record: dict) -> dict:
        snapshot = {
            "approval_set_id": record["approval_set_id"],
            "plan_id": record["plan_id"],
            "plan_version": record["plan_version"],
            "plan_digest": record["plan_digest"],
            "required_actions": copy.deepcopy(record["required_actions"]),
            "approvals": copy.deepcopy(record["approvals"]),
            "is_complete": True,
            "aggregate_status": self._aggregate_status(record),
            "channel": "prototype_ui",
        }
        return snapshot

    @staticmethod
    def _aggregate_status(record: dict) -> str:
        statuses = [approval["status"] for approval in record["approvals"]]
        if "REJECTED" in statuses:
            return "REJECTED"
        if "EXPIRED" in statuses:
            return "EXPIRED"
        if record["invalidated"]:
            return "INVALIDATED"
        if statuses and all(status == "APPROVED" for status in statuses):
            return "APPROVED"
        return "PENDING"

    @staticmethod
    def _refresh_expiry(record: dict, now: datetime) -> bool:
        changed = False
        for approval in record["approvals"]:
            if approval["status"] == "PENDING" and now >= _parse_time(
                approval["expires_at"], "expires_at"
            ):
                approval["status"] = "EXPIRED"
                changed = True
        return changed

    def _bound_set(
        self, approval_set_id: str, plan_id: str, plan_version: int, plan_digest: str
    ) -> dict:
        record = self._sets.get(approval_set_id)
        if record is None:
            raise ApprovalStateNotFoundError(approval_set_id)
        if plan_version != record["plan_version"]:
            raise VersionConflictError(plan_id, plan_version, record["plan_version"])
        if plan_digest != record["plan_digest"]:
            raise DigestMismatchError(
                plan_id, plan_version, plan_digest, record["plan_digest"]
            )
        if plan_id != record["plan_id"]:
            raise ApprovalStateNotFoundError(approval_set_id)
        return record

    def _find_request(self, approval_request_id: str) -> tuple[str, int]:
        for set_id in sorted(self._sets):
            for index, approval in enumerate(self._sets[set_id]["approvals"]):
                if approval["approval_request_id"] == approval_request_id:
                    return set_id, index
        raise ApprovalStateNotFoundError(approval_request_id)

    def __len__(self) -> int:
        return len(self._sets)


__all__ = ["ApprovalService"]
