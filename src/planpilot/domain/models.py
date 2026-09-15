from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

@dataclass(frozen=True)
class Operation:
    operation_no: int; machine_id: str; duration_minutes: int; worker_id: str | None = None
    material_id: str | None = None; material_qty: int = 0; release_at: int = 0; changeover_minutes: int = 0
    required_skill: str | None = None

@dataclass(frozen=True)
class Order:
    order_id: str; product_id: str; quantity: int; due_at: int; operations: tuple[Operation, ...]; priority: int = 0

@dataclass(frozen=True)
class FactoryData:
    orders: tuple[Order, ...]; inventory: dict[str, int] = field(default_factory=dict)
    maintenance: tuple[dict[str, int | str], ...] = (); workers: tuple[dict[str, Any], ...] = (); shifts: tuple[dict[str, int | str], ...] = ()
    machines: tuple[dict[str, Any], ...] = ()
    horizon: int = 7200

@dataclass
class SchedulePlan:
    profile: str; operations: list[dict[str, Any]]; unscheduled_operations: list[dict[str, Any]]; kpis: dict[str, Any]; violations: list[dict[str, Any]] = field(default_factory=list)
    material_reservations: dict[str, Any] = field(default_factory=dict)
    required_actions: list[str] = field(default_factory=list)
    solver_status: str = "UNKNOWN"
