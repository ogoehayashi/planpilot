"""Canonical serialization and SHA-256 digest for immutable plan content.

Contract sources (planpilot_agent_contract_v1.8.json):

  $defs.plan_content.properties.plan_digest.description
    "SHA-256 of the canonical immutable content excluding only plan_digest and
     engine.canonical_plan_hash; equals engine.canonical_plan_hash"

  scheduling_engine.determinism.canonical_serialization
    "Sort operations by start_time, machine_id, order_id, lot_no and
     operation_no; serialize datetimes as RFC 3339 with +08:00. Hash only the
     immutable plan_content object, excluding plan_content.plan_digest and
     plan_content.engine.canonical_plan_hash to avoid circularity. Lifecycle and
     observed runtime fields are separate records and never enter the digest."

Every serialization choice is pinned in .kiro/specs/plan-store-and-digest/design.md
§2.1 and asserted by tests, because leaving any of them implicit would make the
digest unreproducible on another machine — which would falsify
plan_store.retention ("digests are recomputable from stored content at any time").

These are PURE functions. They must not read a clock, a file, or an environment
variable. A digest that depends on anything but its argument is not a digest.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .errors import CanonicalizationError, DigestMismatchError

__all__ = [
    "OPERATION_SORT_KEY_FIELDS",
    "DIGEST_EXCLUDED_FIELDS",
    "ENGINE_DIGEST_EXCLUDED_FIELDS",
    "canonical_json",
    "sort_operations",
    "canonical_plan_digest",
    "assert_digest_consistent",
]

# scheduling_engine.determinism.canonical_serialization — the sort key, in order.
OPERATION_SORT_KEY_FIELDS: tuple[str, ...] = (
    "start_time",
    "machine_id",
    "order_id",
    "lot_no",
    "operation_no",
)

# The two fields excluded to avoid circularity. Nothing else is excluded.
DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"plan_digest"})
ENGINE_DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"canonical_plan_hash"})


def _reject_non_finite(obj: Any, path: str, entity_id: str | None) -> None:
    """Raise if any float in the tree is NaN/Inf.

    json.dumps emits `NaN` and `Infinity` by default. Those are not valid JSON:
    a strict parser rejects them, and a lenient one may read them differently.
    Either way the digest would not be reproducible, so the hole is closed here
    rather than left to the caller.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise CanonicalizationError(
                f"non-finite float {obj!r} at {path} cannot be canonically serialized",
                json_path=path,
                entity_id=entity_id,
            )
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _reject_non_finite(v, f"{path}.{k}" if path else str(k), entity_id)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_non_finite(v, f"{path}[{i}]", entity_id)


def canonical_json(obj: Any, *, _path: str = "", _entity_id: str | None = None) -> str:
    """Serialize to the one canonical JSON text form used for every digest.

    Pinned choices (design.md §2.1):
      encoding        UTF-8            cross-platform
      sort_keys       True             dict insertion order must not matter
      separators      (",", ":")       no whitespace ambiguity
      ensure_ascii    False            non-ASCII hashes as itself; True would
                                       silently expand every non-ASCII string
                                       into \\uXXXX escapes
      non-finite      rejected         see _reject_non_finite
      int vs float    type-preserving  json keeps `1` and `1.0` distinct, which
                                       is correct: $defs.kpis separates
                                       `integer` from `number`
    """
    _reject_non_finite(obj, _path, _entity_id)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,  # defence in depth: json itself raises on NaN/Inf
    )


def sort_operations(operations: list[dict]) -> list[dict]:
    """Return operations ordered by the contract's sort key.

    The key is `(start_time, machine_id, order_id, lot_no, operation_no)`.
    `start_time` is RFC 3339 with a fixed `+08:00` offset, so lexicographic
    comparison of the string equals chronological comparison — no parsing, and
    therefore no dependence on the host locale or timezone database.

    Python's sort is stable, so operations that tie on the full key keep their
    input order. Ties on all five fields mean genuinely identical placement,
    which the contract permits.

    Missing keys sort as empty string / 0 rather than raising: the digest layer
    must not re-implement schema validation (that is validation/factory_state.py
    and validate_plan). A malformed operation still produces a deterministic
    digest; schema rejection happens elsewhere.
    """
    def key(op: dict) -> tuple:
        return tuple(
            op.get(f, "" if f in ("start_time", "machine_id", "order_id") else 0)
            for f in OPERATION_SORT_KEY_FIELDS
        )

    return sorted(operations, key=key)


def canonical_plan_digest(content: dict) -> str:
    """SHA-256 hex digest of immutable plan content.

    Excludes exactly `plan_digest` and `engine.canonical_plan_hash` (circularity).
    Excludes nothing else. Lifecycle and observed runtime fields live in separate
    records and are never passed in — if a caller hands them over anyway they
    would be hashed, which is why PlanStore keeps the two records apart.

    Operations are sorted before hashing, so input order cannot change the digest.
    The input dict is never mutated.
    """
    payload = {k: v for k, v in content.items() if k not in DIGEST_EXCLUDED_FIELDS}

    engine = payload.get("engine")
    if isinstance(engine, dict):
        payload["engine"] = {
            k: v for k, v in engine.items() if k not in ENGINE_DIGEST_EXCLUDED_FIELDS
        }

    ops = payload.get("operations")
    if isinstance(ops, list):
        payload["operations"] = sort_operations(ops)

    entity_id = content.get("plan_id")
    text = canonical_json(payload, _path="plan_content", _entity_id=entity_id)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assert_digest_consistent(content: dict) -> str:
    """Verify both digest identities and return the recomputed digest.

    $defs.plan_content says the digest "equals engine.canonical_plan_hash", and
    plan_digest is the content-addressed key. So both must hold:

        canonical_plan_digest(content) == content["plan_digest"]
        canonical_plan_digest(content) == content["engine"]["canonical_plan_hash"]

    Raises DigestMismatchError with the contract-shaped details
    ($defs.error_details_plan_digest_mismatch: expected_plan_digest,
    recomputed_plan_digest).

    A plan whose digest does not recompute is exactly what a fabricated or
    corrupted tool result looks like, so this is checked on write, not on read.
    """
    recomputed = canonical_plan_digest(content)
    declared = content.get("plan_digest")

    if declared != recomputed:
        raise DigestMismatchError(
            plan_id=str(content.get("plan_id", "")),
            plan_version=int(content.get("plan_version", -1)),
            expected_plan_digest=str(declared),
            recomputed_plan_digest=recomputed,
        )

    engine = content.get("engine") or {}
    canonical_hash = engine.get("canonical_plan_hash") if isinstance(engine, dict) else None
    if canonical_hash != recomputed:
        raise DigestMismatchError(
            plan_id=str(content.get("plan_id", "")),
            plan_version=int(content.get("plan_version", -1)),
            expected_plan_digest=str(canonical_hash),
            recomputed_plan_digest=recomputed,
        )

    return recomputed
