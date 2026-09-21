"""G2 publisher — Phase 1 slice: the idempotency probe (design §4 steps 3–4).

The full publication transaction (§4 steps 5–12, the §4.5
PublisherPreparedCall state machine and the middleware/HTTP wiring) is
Phase 2. Until then NOTHING in the production path imports this module;
what lives here is deliberately small and honest:

- :func:`request_fingerprint` — SHA-256 over ``canonical()`` of the exact
  five-field ``publish_plan`` request. ``canonical`` is the same function
  the audit chain uses, so "identical request" has exactly one definition
  in this codebase (design §3 design facts).
- :class:`PublishIdempotencyConflictError` — the publish variant of the
  registry conflict with an EXPLICIT client ``idempotency_key``
  (design §2 item 4: extend ``IdempotencyConflictError``, do not invent a
  new code). It stays a ``StoreError``, so ``adapt_exception`` transports
  it unchanged: non-retryable ``IDEMPOTENCY_CONFLICT`` whose details are
  exactly ``$defs.error_details_idempotency_conflict``.
- :class:`PublisherService` — steps 3–4 live in
  :meth:`PublisherService._probe_in_open_transaction`, which performs NO
  transaction management of its own (reviewer round-4 probe 2: the
  Phase 2 single-BEGIN flow calls it inside the transaction the §4.5
  state machine has already opened — the body only checks that *a*
  transaction is open, NOT its mode or lock ownership; that guarantee is
  the PreparedCall's job, reviewer round-5 P2). Opening a second
  transaction inside one is an
  ``sqlite3.OperationalError``). The public :meth:`PublisherService.probe`
  is the Phase 1 standalone entry: it opens exactly one transaction and
  delegates.
- Before a ``"replay"`` is returned, the request ↔ receipt ↔ stored
  response are checked as a three-way binding
  (:func:`replay_binding_violation`). A stored publication that
  contradicts itself or the replayed request raises
  :class:`PublicationInvariantError` — persisted-state corruption, the
  ``ApprovalInvariantError`` precedent: middleware fall-through
  (tools/errors.py:70) transports it as contract-shaped
  ``INTERNAL_ERROR`` (retryability per the registry), never as a
  verbatim replay of a contradictory response (reviewer round-4 probe 3).

Both tables are touched ONLY under ``Database.lock`` / ``transaction()``
— the same discipline as ``clock_session`` (design §3, G1.0.2 lesson).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .persistence import canonical
from .store.errors import IdempotencyConflictError, StoreInvariantError

TOOL_NAME = "publish_plan"

#: $defs tool publish_plan input_schema.required — the fingerprint covers
#: exactly these fields, key included (design §3: "request side, 5 fields
#: verbatim; key included").
REQUEST_FIELDS = (
    "plan_id",
    "expected_plan_version",
    "plan_digest",
    "approval_set_id",
    "idempotency_key",
)

#: publish_plan.output_schema properties (contract, additionalProperties
#: false) — the stored response_json must satisfy exactly this key set.
RESPONSE_FIELDS = frozenset(
    {"plan_id", "published_version", "status", "audit_log_id"}
)


def request_fingerprint(request: Mapping[str, Any]) -> str:
    """sha256(canonical(exact 5-field request)) — hex digest.

    Key-order-independent by construction (canonical sorts keys).
    Any field change, including the idempotency key itself, changes the
    digest. Missing or extra fields are a caller defect: the middleware
    has already validated the input schema upstream, so reaching this
    function with a malformed request is structurally impossible and
    crashes as a store invariant violation (never rendered as a
    tool_error — see ``StoreInvariantError`` docstring).
    """
    missing = [field for field in REQUEST_FIELDS if field not in request]
    extra = [key for key in request if key not in REQUEST_FIELDS]
    if missing or extra:
        raise StoreInvariantError(
            "publish request fingerprint requires exactly the 5 contract "
            f"fields (missing={missing}, extra={extra})"
        )
    exact = {field: request[field] for field in REQUEST_FIELDS}
    return hashlib.sha256(canonical(exact).encode("utf-8")).hexdigest()


class PublishIdempotencyConflictError(IdempotencyConflictError):
    """Registry key reused by a DIFFERENT publish request (§5 case B).

    The store's own :class:`IdempotencyConflictError` fabricates
    ``idempotency_key`` as ``"{plan_id}:{plan_version}"`` because that is
    the store's internal write-once key. A client of ``publish_plan``
    supplied a real 16–128 character key and the contract demands THAT
    back in the error details, so this subclass keeps the same code and
    details shape but takes all three detail values explicitly.
    """

    def __init__(
        self,
        idempotency_key: str,
        original_plan_id: str,
        original_status: str | None,
        *,
        original_plan_version: int | None = None,
    ) -> None:
        # StoreError.__init__: message + contract-shaped details, no
        # fabricated key. Details carry EXACTLY the three required fields
        # ($defs.error_details_idempotency_conflict, additionalProperties
        # false) — nothing else may leak in.
        super(IdempotencyConflictError, self).__init__(
            f"idempotency key {idempotency_key!r} is already registered to "
            f"a different {TOOL_NAME} request (original plan "
            f"{original_plan_id!r})",
            {
                "idempotency_key": idempotency_key,
                "original_plan_id": original_plan_id,
                "original_status": original_status,
            },
        )
        self.plan_id = original_plan_id
        self.plan_version = original_plan_version
        self.idempotency_key = idempotency_key


class PublicationInvariantError(Exception):
    """Persisted publication state contradicts itself or the request.

    Same species as ``ApprovalInvariantError`` ("caller or
    persisted-state defect with no truthful registered tool code"): the
    receipt table is asserting something the response disagrees with,
    and no registered code honestly describes a lying database. It
    deliberately does NOT subclass StoreError — middleware fall-through
    (tools/errors.py:70) transports it as contract-shaped
    INTERNAL_ERROR (retryable per the registry), and the in-flight
    transaction rolls back, so a contradictory replay never escapes as
    a 200. Reviewer round-4 probe 3.
    """


def replay_binding_violation(
    request: Mapping[str, Any],
    receipt: Mapping[str, Any],
    response: Any,
) -> str | None:
    """Three-way binding check for case A replays. None = consistent.

    ``receipt`` is the joined registry+receipt row (plan_id,
    plan_version, plan_digest, approval_set_id, audit_log_id);
    ``response`` is the parsed ``response_json``. Checks, in order:

    1. response satisfies publish_plan.output_schema's key set, types
       and the ``status: "PUBLISHED"`` const;
    2. response ↔ receipt: plan_id, audit_log_id and
       published_version==plan_version agree;
    3. request ↔ receipt: the fingerprint already proved byte equality
       of the 5 fields, so plan_id/digest/approval_set and the
       expected_plan_version ↔ plan_version optimistic-concurrency pair
       must match the receipt — a mismatch means the stored rows were
       tampered with after commit (or never told one story).
    """
    if not isinstance(response, dict):
        return "stored response_json is not an object"
    if frozenset(response) != RESPONSE_FIELDS:
        return (
            "stored response keys do not match publish_plan.output_schema "
            f"(have {sorted(response)}, want {sorted(RESPONSE_FIELDS)})"
        )
    if response["status"] != "PUBLISHED":
        return f'stored response status {response["status"]!r} is not "PUBLISHED"'
    if not isinstance(response["plan_id"], str):
        return "stored response plan_id is not a string"
    if isinstance(response["published_version"], bool) or not isinstance(
        response["published_version"], int
    ):
        return "stored response published_version is not an integer"
    if not isinstance(response["audit_log_id"], str):
        return "stored response audit_log_id is not a string"
    if response["plan_id"] != receipt["plan_id"]:
        return (
            f'response plan_id {response["plan_id"]!r} contradicts receipt '
            f'plan_id {receipt["plan_id"]!r}'
        )
    if response["audit_log_id"] != receipt["audit_log_id"]:
        return (
            f'response audit_log_id {response["audit_log_id"]!r} contradicts '
            f'receipt audit_log_id {receipt["audit_log_id"]!r}'
        )
    if response["published_version"] != receipt["plan_version"]:
        return (
            f'response published_version {response["published_version"]!r} '
            f'contradicts receipt plan_version {receipt["plan_version"]!r}'
        )
    if request["plan_id"] != receipt["plan_id"]:
        return "request plan_id contradicts receipt plan_id"
    if request["plan_digest"] != receipt["plan_digest"]:
        return (
            f"request plan_digest {request['plan_digest'][:12]}… contradicts "
            f"receipt plan_digest {receipt['plan_digest'][:12]}…"
        )
    if request["approval_set_id"] != receipt["approval_set_id"]:
        return (
            f"request approval_set_id {request['approval_set_id']!r} "
            f"contradicts receipt approval_set_id "
            f'{receipt["approval_set_id"]!r}'
        )
    if request["expected_plan_version"] != receipt["plan_version"]:
        # Optimistic-concurrency boundary (reviewer round-5 P1): a
        # registry+receipt+response trio may not jointly tell a
        # "requested version 4, actually published version 3" story —
        # the fingerprint hit proves byte equality of the request, so a
        # mismatch here means the stored rows contradict the very
        # precondition the caller committed to.
        return (
            f"request expected_plan_version {request['expected_plan_version']!r} "
            f"contradicts receipt plan_version {receipt['plan_version']!r}"
        )
    return None


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of the steps 3–4 probe.

    ``kind``:
      - ``"replay"`` — case A: ``response`` is the stored receipt
        response, returned verbatim (same audit_log_id, same
        published_version), after the three-way binding check passed.
      - ``"first"`` — key not registered; only Phase 2 may continue to
        steps 5–12. In Phase 1 nothing else happens for this result.
    """

    kind: str
    response: dict[str, Any] | None


class PublisherService:
    """Owns the publication tables and (from Phase 2) the 12-step
    transaction. Phase 1 implements the idempotency probe only."""

    def __init__(self, database, authority) -> None:
        self._database = database
        # authority is the RuntimeAuthority the FULL transaction (steps
        # 5–12, §4.5 staged reload/commit) will synchronise. The probe
        # path must never touch it — Phase 1 pins that fact.
        self._authority = authority

    def probe(self, request: Mapping[str, Any]) -> ProbeResult:
        """Phase 1 standalone entry: open exactly ONE transaction,
        delegate to the body, let the context manager commit/roll back.

        A conflict raises inside the ``transaction()`` context manager,
        which rolls the BEGIN back — structurally guaranteeing "zero
        business change" (§5 case B) even though this probe writes
        nothing. Phase 2 must NOT call this inside an open transaction
        (a second BEGIN raises ``sqlite3.OperationalError``); it calls
        :meth:`_probe_in_open_transaction` instead.
        """
        fingerprint = request_fingerprint(request)
        db = self._database
        with db.transaction():
            return self._probe_in_open_transaction(request, fingerprint)

    def _probe_in_open_transaction(
        self,
        request: Mapping[str, Any],
        fingerprint: str | None = None,
    ) -> ProbeResult:
        """Design §4 steps 3–4 body — performs NO transaction management
        of its own (reviewer round-4 probe 2).

        Its guarantee is exactly one check deep: it asserts that *an*
        open transaction exists on the Database connection (``in_
        transaction``), and nothing more. It CANNOT and DOES NOT verify
        that the transaction is ``BEGIN IMMEDIATE`` or that this thread
        holds ``Database.lock`` — a caller in a plain deferred BEGIN
        outside the lock passes the guard. Those two structural
        guarantees (IMMEDIATE + lock held across the §4.5 stages, and
        released only at the commit point) are the Phase 2
        ``PublisherPreparedCall``'s job, and its tests must prove all
        three: lock ownership, transaction mode, release timing.
        Anything short of an open transaction crashes as a caller
        programming error (``StoreInvariantError``) instead of silently
        half-reading. Reads only: replay/conflict decisions raise or
        return without writing, so the outer transaction's commit point
        stays entirely with Phase 2.
        """
        if fingerprint is None:
            fingerprint = request_fingerprint(request)
        db = self._database
        if not db.conn.in_transaction:
            raise StoreInvariantError(
                "_probe_in_open_transaction requires the caller's open "
                "transaction (design §4.5; BEGIN IMMEDIATE + Database.lock "
                "are the PreparedCall's guarantee); standalone callers use "
                "PublisherService.probe()"
            )
        row = db.conn.execute(
            "SELECT ir.request_fingerprint AS fingerprint,"
            "       pr.plan_id AS plan_id,"
            "       pr.plan_version AS plan_version,"
            "       pr.plan_digest AS plan_digest,"
            "       pr.approval_set_id AS approval_set_id,"
            "       pr.audit_log_id AS audit_log_id,"
            "       pr.response_json AS response_json"
            "  FROM idempotency_registry ir"
            "  JOIN publication_receipt pr ON pr.id = ir.receipt_id"
            " WHERE ir.tool_name = ? AND ir.idempotency_key = ?",
            (TOOL_NAME, request["idempotency_key"]),
        ).fetchone()
        if row is None:
            return ProbeResult("first", None)
        if row["fingerprint"] != fingerprint:
            stored = json.loads(row["response_json"])
            raise PublishIdempotencyConflictError(
                request["idempotency_key"],
                row["plan_id"],
                stored.get("status") if isinstance(stored, dict) else None,
                original_plan_version=row["plan_version"],
            )
        # Case A: replay ONLY if request ↔ receipt ↔ stored response are
        # one consistent story (reviewer round-4 probe 3). Re-canonicalised
        # bytes equal response_json exactly — receipt is the single success
        # payload (design §3 design fact 2).
        receipt = {
            "plan_id": row["plan_id"],
            "plan_version": row["plan_version"],
            "plan_digest": row["plan_digest"],
            "approval_set_id": row["approval_set_id"],
            "audit_log_id": row["audit_log_id"],
        }
        violation = replay_binding_violation(
            request, receipt, json.loads(row["response_json"])
        )
        if violation is not None:
            raise PublicationInvariantError(
                f"idempotency replay refused — {violation}"
            )
        return ProbeResult("replay", json.loads(row["response_json"]))


__all__ = [
    "TOOL_NAME",
    "REQUEST_FIELDS",
    "RESPONSE_FIELDS",
    "ProbeResult",
    "PublicationInvariantError",
    "PublisherService",
    "PublishIdempotencyConflictError",
    "request_fingerprint",
    "replay_binding_violation",
]
