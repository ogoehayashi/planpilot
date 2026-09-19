"""Contract-owned profile weights for deterministic lexicographic optimization."""
from __future__ import annotations

from functools import lru_cache
import json

from planpilot.validation.schema import contract_path


@lru_cache(maxsize=1)
def profile_weights() -> dict[str, tuple[int, int, int, int]]:
    contract = json.loads(contract_path().read_text(encoding="utf-8"))
    fields = ("delivery_weight", "overtime_weight", "changeover_weight", "stability_weight")
    return {
        profile: tuple(round(value[field] * 100) for field in fields)
        for profile, value in contract["plan_profiles"].items()
        if isinstance(value, dict)
    }
