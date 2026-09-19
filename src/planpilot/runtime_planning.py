"""Runtime composition for generation, independent validation and authority."""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from time import monotonic

from .independent_validator import approval_impacts, validate_plan_content
from .domain.importer import factory_from_dict
from .domain.lots import split_lots
from .domain.planning import PROFILES, build_candidates
from .persistence import now  # noqa: F401  (re-export retained for legacy importers)
from .validation.schema import validate_tool_payload
from .v18_adapter import build_material_reservations, build_plan_content, validate_generation_state


def _state_id(state: dict) -> str:
    order_fields = {"order_id", "product_id", "quantity", "due_at", "priority", "urgent_flag", "operations"}
    operation_fields = {"operation_no", "operation_type", "machine_id", "worker_id", "duration", "required_skill", "release"}
    orders = []
    for order in state["orders"]:
        clean = {key: value for key, value in order.items() if key in order_fields and key != "operations"}
        clean["operations"] = [
            {key: value for key, value in operation.items() if key in operation_fields}
            for operation in order["operations"]
        ]
        orders.append(clean)
    authoritative = {
        key: state[key]
        for key in ("planning_start", "horizon", "products", "inventory", "machines", "workers", "shifts", "changeovers", "assumptions")
    }
    authoritative["orders"] = orders
    authoritative["maintenance"] = state.get("maintenance", [])
    encoded = json.dumps(authoritative, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _plan_id(state_id: str, profile: str) -> str:
    suffix = profile.upper().replace(" ", "-")
    return f"PLAN-{state_id[:16]}-{suffix}"


def _horizon_end(state: dict) -> str:
    origin = datetime.fromisoformat(state["planning_start"].replace("Z", "+00:00"))
    return (origin + timedelta(minutes=state["horizon"])).isoformat(timespec="seconds")


def _solver_state(state: dict) -> tuple[dict, dict[str, tuple[str, int]]]:
    """Expand fixed lots into compact-solver jobs without changing authority ids."""
    expanded = deepcopy(state)
    products = {row["product_id"]: row for row in state["products"]}
    origin = datetime.fromisoformat(state["planning_start"].replace("Z", "+00:00"))
    reservations = {
        (row["order_id"], row["lot_no"]): row
        for row in build_material_reservations(state)
    }
    orders, identities = [], {}
    for order in state["orders"]:
        product = products[order["product_id"]]
        for fixed_lot in split_lots(order["order_id"], order["quantity"], product["max_lot_size"]):
            lot_no = fixed_lot.lot_no
            lot = deepcopy(order)
            lot_quantity = fixed_lot.quantity
            virtual_id = f'{order["order_id"]}::LOT::{lot_no}'
            identities[virtual_id] = (order["order_id"], lot_no)
            lot["order_id"] = virtual_id
            lot["quantity"] = lot_quantity
            for operation in lot["operations"]:
                operation.pop("material_id", None)
                operation.pop("material_qty", None)
            first = next(op for op in lot["operations"] if op["operation_type"] == "PRODUCTION")
            reservation = reservations[(order["order_id"], lot_no)]
            if reservation["status"] == "SHORTAGE":
                first["release"] = state["horizon"] + 1
            else:
                ready = datetime.fromisoformat(reservation["earliest_material_ready_time"].replace("Z", "+00:00"))
                first["release"] = max(first.get("release", 0), int((ready - origin).total_seconds() // 60))
            orders.append(lot)
    expanded["orders"] = orders
    return expanded, identities


def _restore_candidate(candidate: dict, identities: dict[str, tuple[str, int]]) -> dict:
    for collection in ("operations", "unscheduled_operations"):
        for row in candidate[collection]:
            order_id, lot_no = identities[row["order_id"]]
            row["order_id"] = order_id
            row["lot_no"] = lot_no
    return candidate


def generate_authoritative_plans(
    state: dict, authority, validation: dict | None = None,
    baseline_plan_id: str | None = None,
) -> dict:
    """Generate three candidates and store only independently validated content."""
    validate_generation_state(state)
    factory_validation = validation or {}
    quarantine_impact = [
        str(issue["entity_id"]) for issue in factory_validation.get("quarantined_entities", [])
        if issue.get("entity_id") is not None
    ]
    solver_state, identities = _solver_state(state)
    factory = factory_from_dict(solver_state)
    state_id = _state_id(state)
    origin = datetime.fromisoformat(state["planning_start"].replace("Z", "+00:00"))
    if baseline_plan_id is not None:
        baseline = authority.get_plan(baseline_plan_id)
        if baseline is None:
            raise ValueError("explicit stability baseline plan does not exist")
        references = {profile: baseline for profile in PROFILES}
    else:
        references = {}
        for profile in PROFILES:
            prior = authority.get_plan(_plan_id(state_id, profile))
            references[profile] = prior if prior and prior["lifecycle"]["status"] == "PUBLISHED" else None
    current_operations = {
        (order["order_id"], operation["operation_no"]): (
            order["product_id"], order["quantity"], operation["operation_type"],
            operation["duration"], operation["machine_id"], operation["worker_id"],
        )
        for order in solver_state["orders"] for operation in order["operations"]
    }
    reference_starts = {}
    for profile, record in references.items():
        if record is None:
            continue
        reference_starts[profile] = {
            (f'{row["order_id"]}::LOT::{row["lot_no"]}', row["operation_no"]):
            int((datetime.fromisoformat(row["start_time"].replace("Z", "+00:00")) - origin).total_seconds() // 60)
            for row in record["content"]["operations"]
            if current_operations.get((f'{row["order_id"]}::LOT::{row["lot_no"]}', row["operation_no"]))
            == (row["product_id"], row["lot_quantity"], row["operation_type"],
                row["duration_min"], row["machine_id"], row["worker_id"])
        }
    started = monotonic()
    candidates = [_restore_candidate(asdict(plan), identities) for plan in build_candidates(
        factory, reference_starts, state["assumptions"]["stability_drift_min"]
    )]
    summaries, installed, per_profile = [], [], {}
    for candidate in candidates:
        profile_started = monotonic()
        plan_id = _plan_id(state_id, candidate["profile"])
        version = authority.next_version(plan_id)
        reference_content = references[candidate["profile"]]
        reference_content = None if reference_content is None else reference_content["content"]
        content = build_plan_content(candidate, state, plan_id, version, reference_content)
        stored = authority.install_generated_plan(
            content,
            lambda immutable: validate_plan_content(
                immutable, state, reference_content=reference_content,
                quarantine_impact=quarantine_impact
            ),
            lambda result: approval_impacts(result, content),
            _horizon_end(state), authority.clock.now(), actor="runtime-planner",
        )
        plan_validation = stored["validation_result"]
        installed.append(stored)
        summaries.append({
            "plan_id": content["plan_id"], "plan_version": content["plan_version"],
            "plan_digest": content["plan_digest"], "profile": content["profile"],
            "generator_feasible": True, "kpis": content["kpis"], "engine": content["engine"],
            "approval_requirements": plan_validation["approval_requirements"],
            "changed_operation_count": 0,
            "affected_order_ids": sorted({row["order_id"] for row in content["operations"]}),
            "quarantined_entity_count": len(factory_validation.get("quarantined_entities", [])), "details_handle": content["plan_id"],
            "summary_truncated": False,
        })
        per_profile[candidate["profile"]] = monotonic() - profile_started
    tool_output = {
        "plan_options": summaries,
        "stored_plan_count": len(summaries),
        "solver_runtime_seconds": monotonic() - started,
        "per_profile_runtime_seconds": per_profile,
    }
    validate_tool_payload(
        tool_output, "generate_plan_options", "output_schema", "generate_plan_options_output"
    )
    recommended_index = max(
        range(len(summaries)),
        key=lambda index: (
            summaries[index]["kpis"]["eligible_order_coverage_rate"],
            summaries[index]["kpis"]["on_time_rate"],
            -summaries[index]["kpis"]["total_tardiness_min"],
            -summaries[index]["kpis"]["total_changeover_min"],
            -index,
        ),
    )
    recommended = installed[recommended_index]
    render_candidates = []
    for candidate, stored in zip(candidates, installed):
        render_candidates.append({
            **candidate,
            "plan_id": stored["content"]["plan_id"],
            "plan_version": stored["content"]["plan_version"],
            "plan_digest": stored["content"]["plan_digest"],
        })
    return {
        **tool_output,
        "state": "PLANS_VALIDATED",
        "plan_id": recommended["content"]["plan_id"],
        "version": recommended["content"]["plan_version"],
        "plan_digest": recommended["content"]["plan_digest"],
        "content": recommended["content"],
        "lifecycle": recommended["lifecycle"],
        "candidates": render_candidates,
        "quarantine_impact": quarantine_impact,
    }


def generate_authoritative_plans_from_state(
    state_id: str, registry, authority, baseline_plan_id: str | None = None,
) -> dict:
    """Production tool boundary: resolve immutable state by reference only."""
    record = registry.get(state_id)
    if record["validation"]["status"] == "INVALID":
        raise ValueError("factory state is structurally INVALID")
    result = generate_authoritative_plans(
        record["state"], authority, record["validation"], baseline_plan_id
    )
    result["state_id"] = state_id
    return result
