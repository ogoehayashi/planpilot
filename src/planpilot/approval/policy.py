"""Closed approval policy derived from the V1.8 contract.

This module contains no judgement and no LLM call.  Constants are mirrored for
fast deterministic service logic and are pinned against the contract by tests.
"""

from __future__ import annotations

APPROVAL_ACTIONS: tuple[str, ...] = (
    "assign_qualified_secondary_skill",
    "add_overtime",
    "change_promised_due_date",
    "publish_plan",
)

ROLE_BY_ACTION: dict[str, str] = {
    "assign_qualified_secondary_skill": "Production Planner",
    "add_overtime": "Production Manager",
    "change_promised_due_date": "Production Manager",
    "publish_plan": "Production Planner",
}

REASON_BY_ACTION: dict[str, str] = {
    "assign_qualified_secondary_skill": "SECONDARY_SKILL_REQUIRED",
    "add_overtime": "OVERTIME_REQUIRED",
    "change_promised_due_date": "PROMISED_DUE_DATE_CHANGE_REQUIRED",
    "publish_plan": "PUBLISH_CONFIRMATION_REQUIRED",
}

DEFAULT_TTL_SECONDS: dict[str, int] = {
    "publish_plan": 86400,
    "add_overtime": 14400,
    "change_promised_due_date": 14400,
    "assign_qualified_secondary_skill": 7200,
}

MINIMUM_TTL_SECONDS = 300
MAXIMUM_TTL_SECONDS = 172800

DECISION_REASON_CODES: frozenset[str] = frozenset(
    {
        "OVERTIME_NOT_JUSTIFIED",
        "OVERTIME_BUDGET_EXCEEDED",
        "DUE_DATE_COMMITMENT_UNACCEPTABLE",
        "CUSTOMER_NOT_CONSULTED",
        "SECONDARY_SKILL_NOT_AUTHORISED",
        "PLAN_NOT_REVIEWED_YET",
        "PREFER_ALTERNATIVE_PROFILE",
        "DATA_ASSUMPTION_WRONG",
        "PRIORITY_CHANGED",
        "OTHER_SEE_COMMENT",
    }
)

INVALIDATION_CAUSES: frozenset[str] = frozenset(
    {
        "plan_regenerated",
        "plan_content_mutated",
        "kpi_changed",
        "digest_changed",
        "superseded_version",
    }
)


def ordered_actions(actions) -> list[str]:
    """Return unique approval actions in the contract's deterministic order."""
    values = set(actions)
    unknown = values.difference(APPROVAL_ACTIONS)
    if unknown:
        raise ValueError(f"unknown approval action(s): {sorted(unknown)}")
    return [action for action in APPROVAL_ACTIONS if action in values]


__all__ = [
    "APPROVAL_ACTIONS",
    "ROLE_BY_ACTION",
    "REASON_BY_ACTION",
    "DEFAULT_TTL_SECONDS",
    "MINIMUM_TTL_SECONDS",
    "MAXIMUM_TTL_SECONDS",
    "DECISION_REASON_CODES",
    "INVALIDATION_CAUSES",
    "ordered_actions",
]

