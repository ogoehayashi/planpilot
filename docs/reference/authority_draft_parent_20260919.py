"""Authority bridge: the audited PlanStore + ApprovalService become the single
source of truth for versions, approvals, publication and audit.

The legacy ``Database`` is demoted to a staging cache — it still holds the
free-form ``{candidates, factory_data}`` payload that ``/schedule`` answers
with and that ``GET /plans`` reads back — but every versioned authority write
(content materialisation, approval sets, publication) goes through the
contract-validated ``PlanStore`` and the deterministic ``ApprovalService``.

Non-contract payloads are rejected here before staging ever sees them: the
old ``Database.save_plan({"totally_made_up": true})`` acceptance is gone.

Why the facade keeps Database's seven method shapes: ``tools/api_server.py``
and the remaining ``Database`` consumers (chat, bedrock client, harness
tools) switch to this class without touching call sites; authority semantics
change underneath, wire semantics do not.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

from .approval import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalRequiredError,
    ApprovalService,
    ApprovalSetInvalidatedError,
    ApprovalStateNotFoundError,
    ROLE_BY_ACTION,
)
from .approval.policy import REASON_BY_ACTION, ordered_actions
from .domain.importer import factory_from_dict
from .domain.planning import is_secondary, reserve_materials, validate_plan
from .persistence import Database, now
from .store import canonical_plan_digest, PlanStore
from .store.errors import DigestMismatchError, PlanNotFoundError, VersionConflictError

_SGT = timezone(timedelta(hours=8))
_COMPACT_ROLE = {"Production Manager": "manager", "Production Planner": "planner"}
_PERMISSION_BY_ACTION = {
    "publish_plan": "approve_publish",
    "add_overtime": "approve_overtime",
    "change_promised_due_date": "approve_due_date",
    "assign_qualified_secondary_skill": "approve_secondary",
}
_SOLVER_STATUSES = ("OPTIMAL", "FEASIBLE", "TIME_LIMIT_FEASIBLE", "HEURISTIC_FALLBACK")
_UNSCHEDULED_REASON_CODES = (
    ("material", "MATERIAL_SHORTAGE"),
    ("machine", "MACHINE_UNAVAILABLE"),
    ("worker", "WORKER_UNAVAILABLE"),
    ("skill", "SKILL_GAP"),
    ("shift", "SHIFT_WINDOW"),
    ("maintenance", "MAINTENANCE_CONFLICT"),
    ("quarantin", "QUARANTINED_ORDER"),
)


def _iso_at(base: datetime, minutes: int) -> str:
    return (base + timedelta(minutes=int(minutes))).isoformat(timespec="seconds")


def candidate_to_plan_content(base, factory_dict, candidate, plan_id, plan_version):
    """Transform one compact-adapter engine candidate into $defs.plan_content.

    The engine schedules integer minutes from ``t0``; the contract speaks RFC
    3339 +08:00. Everything else is a field-for-field mapping validated later
    by PlanStore's schema gate — this function only shapes, never vouches.
    """
    orders = {o["order_id"]: o for o in factory_dict["orders"]}
    operations = []
    for row in candidate["operations"]:
        order = orders[row["order_id"]]
        operations.append({
            "order_id": row["order_id"],
            "product_id": order["product_id"],
            "lot_no": 1,
            "lot_quantity": int(order["quantity"]),
            "operation_no": int(row["operation_no"]),
            "operation_type": "PRODUCTION",
            "machine_id": row["machine_id"],
            "worker_id": row["worker_id"],
            "start_time": _iso_at(base, row["start"]),
            "end_time": _iso_at(base, row["end"]),
            "calendar_window_ids": [
                f"CAL-M-{row['machine_id']}-REG", f"CAL-W-{row['worker_id']}-REG"],
            "duration_min": int(row["duration"]),
            "overtime_min": 0,
            "changeover_min": 0,
            "risk_level": "Low",
            "decision_reason_codes": ["EARLIEST_DUE_DATE", "PRIMARY_SKILL_MATCH"],
            "decision_summary": "Compact adapter deterministic solve.",
        })
    engine_kpis = candidate["kpis"]
    late = engine_kpis["late_orders"]
    kpis = {
        "on_time_rate": float(engine_kpis["on_time_rate"]),
        "eligible_orders": int(engine_kpis["eligible_orders"]),
        "on_time_orders": int(engine_kpis["on_time_orders"]),
        "eligible_order_coverage_rate": float(engine_kpis["eligible_order_coverage_rate"]),
        "late_orders": len(late) if isinstance(late, list) else int(late),
        "total_tardiness_min": int(engine_kpis["total_tardiness_min"]),
        "overtime_hours": float(engine_kpis["overtime_hours"]),
        "changeover_count": int(engine_kpis["changeover_count"]),
        "total_changeover_min": int(engine_kpis["total_changeover_min"]),
        "schedule_stability": 1.0,
        "unscheduled_operations": int(engine_kpis["unscheduled_operations"]),
        "secondary_skill_assignment_count": int(engine_kpis["secondary_skill_assignment_count"]),
    }
    unscheduled = []
    for row in candidate["unscheduled_operations"]:
        reason = str(row.get("reason", ""))
        code = next((c for k, c in _UNSCHEDULED_REASON_CODES if k in reason.lower()), "CAPACITY")
        order = orders.get(row["order_id"], {})
        unscheduled.append({
            "order_id": row["order_id"],
            "product_id": order.get("product_id", "p-unknown"),
            "lot_no": 1,
            "operation_no": int(row["operation_no"]),
            "reason_code": code,
            "reason": reason or "not scheduled",
        })
    reservations = []
    for rank, (oid, res) in enumerate(sorted(candidate["material_reservations"].items()), start=1):
        order = orders[oid]
        by_material: dict[str, list] = {}
        for alloc in res["allocations"]:
            by_material.setdefault(alloc["material_id"], []).append(alloc)
        lines = []
        for material_id, allocs in sorted(by_material.items()):
            required = sum(int(a["quantity"]) for a in allocs)
            lines.append({
                "material_id": material_id,
                "material_uom": "EA",
                "required_quantity_base_units": max(required, 1),
                "reserved_quantity_base_units": required,
                "allocations": [{
                    "source_type": "ON_HAND",
                    "source_id": str(a["batch_id"]),
                    "available_at": _iso_at(base, a["available_at"]),
                    "quantity_base_units": int(a["quantity"]),
                } for a in sorted(allocs, key=lambda x: str(x["batch_id"]))],
            })
        ready = res.get("ready_at")
        reservations.append({
            "order_id": oid,
            "product_id": order["product_id"],
            "lot_no": 1,
            "lot_quantity": int(order["quantity"]),
            "priority_rank": rank,
            "status": res["status"],
            "earliest_material_ready_time": _iso_at(base, ready) if ready is not None else None,
            "lines": lines,
        })
    content = {
        "plan_id": plan_id,
        "plan_version": int(plan_version),
        "plan_digest": "0" * 64,
        "profile": candidate["profile"],
        "operations": operations,
        "unscheduled_operations": unscheduled,
        "kpis": kpis,
        "engine": {
            "solver": "CP-SAT",
            "solver_status": candidate["solver_status"] if candidate["solver_status"] in _SOLVER_STATUSES else "FEASIBLE",
            "random_seed": 0,
            "time_budget_seconds": 32.0,
            "deterministic_budget": 32.0,
            "canonical_plan_hash": "0" * 64,
            "objective_value": None,
            "normalized_scores": {
                "delivery_score": float(kpis["on_time_rate"]),
                "overtime_score": round(1.0 / (1.0 + kpis["overtime_hours"]), 4),
                "changeover_score": round(1.0 / (1.0 + kpis["total_changeover_min"]), 4),
                "stability_score": 1.0,
            },
        },
        "stability_reference_plan_id": None,
        "assumptions": ["Compact adapter solve; horizon anchored at request time, Asia/Singapore +08:00."],
        "consequential_approval_required": True,
        "publish_confirmation_required": True,
        "infeasible_reason": None,
        "material_reservations": reservations,
    }
    digest = canonical_plan_digest(content)
    content["plan_digest"] = digest
    content["engine"]["canonical_plan_hash"] = digest
    return content


class PlanAuthority:
    """Seven-method Database-shaped facade over the audited authority stack."""

    def __init__(self, staging: Database, state_dir: str | Path | None = None):
        self.staging = staging
        self.store = PlanStore()
        self.service = None  # constructed below; kept as attribute for debugging
        if state_dir is None:
            row = staging.conn.execute("PRAGMA database_list").fetchone()
            file = row[2] if row and len(row) > 2 else ""
            if file and file != ":memory:":
                state_dir = Path(str(file) + ".authority")
            else:
                state_dir = Path(tempfile.mkdtemp(prefix="planpilot-authority-"))
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store_path = self._dir / "store.json"
        self._approvals_path = self._dir / "approvals.json"
        if self._store_path.exists():
            self.store.load_state(self._store_path)
        self.service = ApprovalService(self.store, self._approvals_path)
        if self._approvals_path.exists():
            self.service.load_state(self._approvals_path)
        # request_id -> binding metadata, rebuilt from service state so a
        # restart never loses the way back from an approval row.
        self._requests: dict[str, dict] = {}
        self._set_ids: dict[tuple[str, int], str] = {}
        self._publications: dict[tuple[str, int, str], dict] = {}
        self._rebuild_indexes()

    # ------------------------------------------------------------- internals

    def _rebuild_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = self.service.dump_state(Path(tmp) / "snapshot.json")
        state = json.loads(text)
        for record in state.get("approval_sets", []):
            binding = (record["plan_id"], record["plan_version"])
            self._set_ids[binding] = record["approval_set_id"]
            for approval in record["approvals"]:
                self._requests[approval["approval_request_id"]] = {
                    "set_id": record["approval_set_id"],
                    "plan_id": record["plan_id"],
                    "plan_version": record["plan_version"],
                    "plan_digest": record["plan_digest"],
                    "action": approval["action"],
                    "approver_role": approval["approver_role"],
                    "candidate_id": record["required_actions"] and None or None,
                }

    def _persist_store(self) -> None:
        self.store.dump_state(self._store_path)

    def _staging_head(self, plan_id):
        head = self.staging.get_plan(plan_id)
        if head is None:
            raise KeyError("plan not found")
        return head

    def _candidate_row(self, plan_id, candidate_id):
        head = self._staging_head(plan_id)
        payload = head["payload"]
        candidates = [c for c in payload.get("candidates", []) if c.get("profile") == candidate_id]
        if len(candidates) != 1:
            raise ValueError("candidate not found or ambiguous")
        return head, payload, candidates[0]

    def _feasibility_gate(self, payload, candidate):
        """Server-side truth check, exactly the old Database._candidate policy."""
        if candidate.get("violations") or not candidate.get("operations"):
            raise PermissionError("candidate is not validated and feasible")
        factory = factory_from_dict(payload["factory_data"])
        if validate_plan(candidate["operations"], factory, candidate.get("unscheduled_operations", [])):
            raise PermissionError("independent validation failed")
        if candidate.get("material_reservations") != reserve_materials(factory):
            raise PermissionError("material reservation mismatch")
        return factory

    def _derive_actions(self, payload, factory, candidate):
        actions = {"publish_plan"}
        scheduled = {(r["order_id"], r["operation_no"]) for r in candidate["operations"]}
        if any(is_secondary(factory, op) for order in factory.orders
               for op in order.operations if (order.order_id, op.operation_no) in scheduled):
            actions.add("assign_qualified_secondary_skill")
        if candidate.get("kpis", {}).get("overtime_hours", 0) > 0:
            actions.add("add_overtime")
        if payload.get("change_promised_due_date"):
            actions.add("change_promised_due_date")
        return ordered_actions(actions)

    def _validation_payload(self, content, actions, ts, horizon_end):
        plan_id, version = content["plan_id"], content["plan_version"]
        recomputed = self.store.verify_digest(plan_id, version)
        result = {
            "plan_id": plan_id, "plan_version": version,
            "plan_digest": recomputed, "recomputed_plan_digest": recomputed,
            "digest_verified": True, "is_feasible": True, "infeasible_reason": None,
            "hard_violations": [], "kpis": json.loads(json.dumps(content["kpis"])),
            "approval_requirements": [
                {"action": a, "approver_role": ROLE_BY_ACTION[a],
                 "reason": f"Server-derived requirement for {a}."} for a in actions],
            "quarantine_impact": [],
        }
        affected = sorted({op["order_id"] for op in content["operations"]})
        impacts = {a: {
            "affected_order_ids": affected,
            "changed_operation_count": len(content["operations"]),
            "kpi_deltas": ({"overtime_hours": content["kpis"]["overtime_hours"]}
                           if a == "add_overtime" else {}),
            "reason_codes": [REASON_BY_ACTION[a]],
        } for a in actions}
        return result, impacts, horizon_end, ts

    @staticmethod
    def _rows(snapshot, candidate_id):
        return [{
            "request_id": a["approval_request_id"], "id": a["approval_request_id"],
            "plan_id": snapshot["plan_id"], "version": snapshot["plan_version"],
            "digest": snapshot["plan_digest"], "candidate_id": candidate_id,
            "action": a["action"], "role": _COMPACT_ROLE[a["approver_role"]],
            "status": a["status"], "expires_at": a["expires_at"],
            "decided_by": a["decided_by"], "decided_at": a["decided_at"],
        } for a in snapshot["approvals"]]

    # ------------------------------------------------- staging delegation --

    @property
    def conn(self):
        return self.staging.conn

    @property
    def lock(self):
        return self.staging.lock

    def transaction(self):
        return self.staging.transaction()

    def _audit(self, *args):
        return self.staging._audit(*args)

    def audit(self, *args):
        return self.staging.audit(*args)

    def verify_audit(self):
        return self.staging.verify_audit()

    def close(self):
        self.staging.close()

    def get_plan(self, plan_id, version=None):
        return self.staging.get_plan(plan_id, version)

    def save_plan(self, plan_id, payload, version, actor="system"):
        # Structural gate at the new authority boundary: the staging payload
        # must at least be a solve result, never arbitrary JSON.
        if (not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list)
                or not payload["candidates"] or not isinstance(payload.get("factory_data"), dict)):
            raise ValueError("staging payload must carry candidates and factory_data")
        for candidate in payload["candidates"]:
            for field in ("profile", "operations", "kpis", "material_reservations"):
                if field not in candidate:
                    raise ValueError(f"candidate missing required field {field!r}")
        saved = self.staging.save_plan(plan_id, payload, version, actor)
        # A regeneration invalidates any open authority binding for the plan,
        # mirroring the Database rule that stale approvals never publish work.
        for (pid, v), set_id in list(self._set_ids.items()):
            if pid != plan_id or v >= version:
                continue
            try:
                self.service.invalidate_set(
                    set_id, "plan_regenerated", self.store.verify_digest(pid, v))
            except (ApprovalStateNotFoundError, ApprovalError, KeyError, ValueError):
                continue
        self.staging.audit(plan_id, actor, "plan_staged", saved)
        return saved

    # ------------------------------------------------- authority surface ---

    def request_approval(self, plan_id, candidate_id, actions=None, expected_version=None, actor="system"):
        head, payload, candidate = self._candidate_row(plan_id, candidate_id)
        if expected_version is not None and head["version"] != expected_version:
            raise RuntimeError("optimistic concurrency conflict")
        factory = self._feasibility_gate(payload, candidate)
        derived = self._derive_actions(payload, factory, candidate)
        if actions is not None and set(actions) != set(derived):
            raise ValueError("approval actions must match server-derived requirements")
        ts = now()
        base = datetime.fromisoformat(ts)
        horizon_end = _iso_at(base, payload["factory_data"].get("horizon", 0))
        content = candidate_to_plan_content(base, payload["factory_data"], candidate, plan_id, head["version"])
        version = content["plan_version"]
        digest = content["plan_digest"]
        try:
            lifecycle = self.store.get_lifecycle(plan_id, version)
        except PlanNotFoundError:
            lifecycle = None
        if lifecycle is None:
            if self.store.latest_version(plan_id) > 0 if hasattr(self.store, "latest_version") else False:
                self.store.commit_new_version(content, ts)
                try:
                    self.service.consume_superseded(ts)
                except ApprovalError:
                    pass
            else:
                self.store.put_content(content)
                self.store.create_lifecycle(plan_id, version, digest, ts)
        self._persist_store()
        result, impacts, horizon_end, ts = self._validation_payload(content, derived, ts, horizon_end)
        self.service.record_validated_plan(result, impacts, horizon_end, ts)
        snapshot = self.service.request_approval(plan_id, version, digest, derived[0], ts)
        set_id = snapshot["approval_set_id"]
        self._set_ids[(plan_id, version)] = set_id
        for approval in snapshot["approvals"]:
            self._requests[approval["approval_request_id"]] = {
                "set_id": set_id, "plan_id": plan_id, "plan_version": version,
                "plan_digest": digest, "action": approval["action"],
                "approver_role": approval["approver_role"], "candidate_id": candidate_id}
        lifecycle = self.store.get_lifecycle(plan_id, version)
        if lifecycle["status"] == "DRAFT":
            self.store.transition(plan_id, version, "AWAITING_APPROVAL", ts, approval_set_id=set_id)
        self._persist_store()
        self.staging.audit(plan_id, actor, "approval_requested",
                           {"version": version, "candidate_id": candidate_id, "approval_set_id": set_id})
        return self._rows(snapshot, candidate_id)

    def approval_permission(self, request_id):
        entry = self._requests.get(request_id)
        if entry is None:
            raise KeyError("approval request not found")
        return _PERMISSION_BY_ACTION[entry["action"]]

    def decide_approval(self, request_id, actor, role, decision):
        if decision not in ("APPROVED", "REJECTED", "APPROVE", "REJECT"):
            raise ValueError("decision must be APPROVED or REJECTED")
        normalized = "APPROVED" if decision in ("APPROVED", "APPROVE") else "REJECTED"
        entry = self._requests.get(request_id)
        if entry is None:
            raise KeyError("approval request not found")
        if _COMPACT_ROLE[entry["approver_role"]] != role:
            raise PermissionError("role cannot decide this request")
        ts = now()
        try:
            snapshot = self.service.record_decision(
                request_id, normalized, entry["approver_role"], actor, ts,
                decision_reason=(None if normalized == "APPROVED" else "OTHER_SEE_COMMENT"))
        except ApprovalExpiredError as exc:
            raise PermissionError(f"approval expired: {exc}") from exc
        except ApprovalStateNotFoundError as exc:
            raise KeyError("approval request not found") from exc
        except ApprovalError as exc:
            raise self._translate_authority_error(exc) from exc
        if snapshot["aggregate_status"] == "APPROVED":
            lifecycle = self.store.get_lifecycle(entry["plan_id"], entry["plan_version"])
            if lifecycle["status"] == "AWAITING_APPROVAL":
                self.store.transition(entry["plan_id"], entry["plan_version"], "APPROVED", ts,
                                      approval_set_id=entry["set_id"])
                self._persist_store()
        self.staging.audit(entry["plan_id"], actor, "approval_decided",
                           {"request_id": request_id, "decision": normalized})
        return {"request_id": request_id, "status": normalized, "decided_by": actor}

    def publish_plan(self, plan_id, candidate_id, actor, role, expected_version=None):
        if role != "planner":
            raise PermissionError("only planner may publish")
        head, _, _ = self._candidate_row(plan_id, candidate_id)
        version = head["version"]
        if expected_version is not None and version != expected_version:
            raise RuntimeError("optimistic concurrency conflict")
        key = (plan_id, version, candidate_id)
        existing = self._publications.get(key)
        if existing is not None:
            return existing
        set_id = self._set_ids.get((plan_id, version))
        if set_id is None:
            raise PermissionError("all current approval requests must be approved")
        digest = self.store.verify_digest(plan_id, version)
        content = self.store.get_content(plan_id, version)
        if content["profile"] != candidate_id:
            raise PermissionError("all current approval requests must be approved")
        ts = now()
        try:
            snapshot = self.service.require_approved(set_id, plan_id, version, digest, ts)
        except (ApprovalRequiredError, ApprovalRejectedError, ApprovalExpiredError) as exc:
            raise PermissionError(f"all current approval requests must be approved: {exc}") from exc
        except ApprovalSetInvalidatedError as exc:
            raise RuntimeError(f"approval set was invalidated: {exc}") from exc
        except (VersionConflictError, DigestMismatchError) as exc:
            raise RuntimeError(str(exc)) from exc
        except ApprovalError as exc:
            raise self._translate_authority_error(exc) from exc
        lifecycle = self.store.get_lifecycle(plan_id, version)
        if lifecycle["status"] == "AWAITING_APPROVAL":
            self.store.transition(plan_id, version, "APPROVED", ts, approval_set_id=set_id)
        self.store.transition(plan_id, version, "PUBLISHED", ts,
                              approval_set_id=set_id, published_version=version,
                              expected_plan_version=version)
        self._persist_store()
        published = {"plan_id": plan_id, "version": version, "candidate_id": candidate_id,
                     "digest": digest, "actor": actor, "published_at": ts, "status": "PUBLISHED"}
        self._publications[key] = published
        self.staging.audit(plan_id, actor, "plan_published",
                           {"version": version, "candidate_id": candidate_id, "digest": digest})
        return published

    @staticmethod
    def _translate_authority_error(exc: Exception) -> RuntimeError:
        return RuntimeError(str(exc))


__all__ = ["PlanAuthority", "candidate_to_plan_content"]
