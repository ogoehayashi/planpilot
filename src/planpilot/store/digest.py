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

from .errors import DIGEST_RE, CanonicalizationError, DigestMismatchError, InvalidContentError

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
    """Raise if the tree contains NaN/Inf or an isolated UTF-16 surrogate.

    json.dumps emits `NaN` and `Infinity` by default. Those are not valid JSON:
    a strict parser rejects them, and a lenient one may read them differently.
    Either way the digest would not be reproducible, so the hole is closed here
    rather than left to the caller.  With ``ensure_ascii=False``, an isolated
    surrogate also survives ``json.dumps`` but fails later at UTF-8 encoding.
    Detecting it here turns that raw ``UnicodeEncodeError`` into the registered,
    contract-shaped ``CanonicalizationError`` used by the tool boundary.
    """
    if isinstance(obj, str):
        for index, char in enumerate(obj):
            if 0xD800 <= ord(char) <= 0xDFFF:
                raise CanonicalizationError(
                    f"isolated UTF-16 surrogate U+{ord(char):04X} at {path}[{index}] "
                    "cannot be canonically serialized",
                    json_path=f"{path}[{index}]",
                    entity_id=entity_id,
                )
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise CanonicalizationError(
                f"non-finite float {obj!r} at {path} cannot be canonically serialized",
                json_path=path,
                entity_id=entity_id,
            )
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _reject_non_finite(k, f"{path}.<key>" if path else "<key>", entity_id)
            _reject_non_finite(v, f"{path}.{k}" if path else str(k), entity_id)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_non_finite(v, f"{path}[{i}]", entity_id)


def _normalize_negative_zero(obj: Any) -> Any:
    """Return a canonical-value copy in which every ``-0.0`` is ``0.0``.

    IEEE-754 signed zero compares equal and has identical scheduling meaning,
    but Python's JSON encoder emits ``-0.0`` and ``0.0`` differently.  Without
    this normalization, equivalent solver output could produce a false digest
    mismatch.  Containers are copied so canonicalization never mutates input.
    """
    if isinstance(obj, float) and obj == 0.0:
        return 0.0
    if isinstance(obj, dict):
        return {k: _normalize_negative_zero(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_negative_zero(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_normalize_negative_zero(v) for v in obj)
    return obj


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
      signed zero     normalized       -0.0 and 0.0 have one representation
      surrogates      rejected         UTF-8 cannot encode isolated surrogates
      int vs float    type-preserving  json keeps `1` and `1.0` distinct, which
                                       is correct: $defs.kpis separates
                                       `integer` from `number`
    """
    _reject_non_finite(obj, _path, _entity_id)
    normalized = _normalize_negative_zero(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,  # defence in depth: json itself raises on NaN/Inf
    )


# Type ranks for the total-order sort key. bool is ranked separately from int
# (bool is a subclass of int in Python, so it must be tested first). Exact-type
# lookup, with a fallback rank for anything unexpected.
_TYPE_RANK = {bool: 0, int: 1, float: 2, str: 3, type(None): 4}
_TYPE_RANK_FALLBACK = 99


def _total_key(value) -> tuple:
    """A comparison key that never raises, for one sort-key field.

    Returns `(type_rank, comparable)`. Two such keys compare safely because
    Python compares element-wise: when the ranks differ it short-circuits on the
    int rank and never compares the second elements; when the ranks are equal the
    two values are the SAME type, so the native comparison is well-defined.

    Audit finding F12: the bare 5-field key raised
    `TypeError: '<' not supported between str and int` on mixed-type lot_no,
    which broke this module's documented promise (design decision 4) that the
    digest layer is total over canonicalizable JSON values. Inputs with no valid
    UTF-8 JSON form (for example an isolated surrogate) are rejected deliberately.
    For well-formed homogeneous input every field has one type, so the rank is
    constant per position and ordering is unchanged.
    """
    rank = _TYPE_RANK.get(type(value), _TYPE_RANK_FALLBACK)
    if rank == _TYPE_RANK_FALLBACK:
        # dict/list/etc. in a sort field is malformed; canonical_json is always a
        # string, so these stay mutually comparable and never raise against ints.
        return (rank, canonical_json(value))
    return (rank, value)


def sort_operations(operations: list[dict]) -> list[dict]:
    """Return operations ordered by the contract's sort key, as a TOTAL order.

    The contract key is `(start_time, machine_id, order_id, lot_no, operation_no)`.
    `start_time` is RFC 3339 with a fixed `+08:00` offset, so lexicographic
    comparison of the string equals chronological comparison — no parsing, and
    therefore no dependence on the host locale or timezone database.

    Two properties this must hold, both pinned by tests:

    * F4/F12 (order-independence): two operations tied on all five contract
      fields but differing elsewhere must NOT keep input order — that made the
      digest depend on input order and broke plan_store.retention ("digests are
      recomputable from stored content at any time"). Measured before fixing:
      reordering two tied ops changed the digest. Fixed by breaking remaining
      ties with the canonical JSON of the whole operation.

    * F12 (totality): mixed-type key fields (e.g. lot_no 1 vs "1") must not
      raise TypeError. Each field is wrapped in _total_key so cross-type
      comparison short-circuits on a type rank. The order is total for values
      with a canonical UTF-8 JSON representation; unencodable strings raise the
      registered CanonicalizationError before hashing.

    The contract's five-field key still governs the primary order. For
    well-formed input the tiebreaker and the type rank never change the result,
    so the baseline digest is unchanged.
    """
    def key(op: dict) -> tuple:
        primary = tuple(
            _total_key(op.get(f, "" if f in ("start_time", "machine_id", "order_id") else 0))
            for f in OPERATION_SORT_KEY_FIELDS
        )
        # Tiebreaker: canonical JSON of the operation. canonical_json is itself
        # deterministic (sorted keys, no whitespace), so equal content always
        # sorts equal and the order no longer depends on input position.
        return primary + (canonical_json(op),)

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


def _safe_version(content: dict) -> int:
    """plan_version for error reporting only — never for keys or logic.

    The first version called int(content.get("plan_version", -1)) inline, which
    raises ValueError on a non-numeric version and MASKS the DigestMismatchError
    the caller actually needed (audit finding F6). Reporting a version must not
    be able to fail.
    """
    v = content.get("plan_version")
    return v if isinstance(v, int) and not isinstance(v, bool) else -1


def assert_digest_consistent(content: dict) -> str:
    """Verify both digest identities and return the recomputed digest.

    $defs.plan_content says the digest "equals engine.canonical_plan_hash", and
    plan_digest is the content-addressed key. So both must hold:

        canonical_plan_digest(content) == content["plan_digest"]
        canonical_plan_digest(content) == content["engine"]["canonical_plan_hash"]

    Raises DigestMismatchError with the contract-shaped details
    ($defs.error_details_plan_digest_mismatch: expected_plan_digest,
    recomputed_plan_digest) when two WELL-FORMED digests disagree.

    Raises InvalidContentError when a declared digest is missing or malformed:
    that is not a mismatch, and PLAN_DIGEST_MISMATCH's schema requires both sides
    to be 64-hex, so forcing it would build an unemittable tool_error (audit F5).

    A plan whose digest does not recompute is exactly what a fabricated or
    corrupted tool result looks like, so this is checked on write, not on read.
    """
    recomputed = canonical_plan_digest(content)
    declared = content.get("plan_digest")

    if not isinstance(declared, str) or not DIGEST_RE.match(declared):
        raise InvalidContentError(
            str(content.get("plan_id", "")),
            _safe_version(content),
            json_path="plan_digest",
            reason=f"declared digest {declared!r} is not 64-char lowercase hex",
        )
    if declared != recomputed:
        raise DigestMismatchError(
            plan_id=str(content.get("plan_id", "")),
            plan_version=_safe_version(content),
            expected_plan_digest=declared,
            recomputed_plan_digest=recomputed,
        )

    engine = content.get("engine") or {}
    canonical_hash = engine.get("canonical_plan_hash") if isinstance(engine, dict) else None
    if not isinstance(canonical_hash, str) or not DIGEST_RE.match(canonical_hash):
        raise InvalidContentError(
            str(content.get("plan_id", "")),
            _safe_version(content),
            json_path="engine.canonical_plan_hash",
            reason=f"engine hash {canonical_hash!r} is not 64-char lowercase hex",
        )
    if canonical_hash != recomputed:
        raise DigestMismatchError(
            plan_id=str(content.get("plan_id", "")),
            plan_version=_safe_version(content),
            expected_plan_digest=canonical_hash,
            recomputed_plan_digest=recomputed,
        )

    return recomputed
