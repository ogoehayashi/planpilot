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
- :class:`PublisherService` — :meth:`PublisherService.probe` performs
  steps 3–4 ONLY: open the transaction, SELECT the registry row for
  (tool_name, idempotency_key); hit + fingerprint match → replay the
  stored receipt response verbatim; hit + different fingerprint → raise
  (the transaction context manager rolls back → zero business change).
  A miss returns ``kind="first"`` for Phase 2 to continue with steps
  5–12; in Phase 1 a miss writes nothing.

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


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of the steps 3–4 probe.

    ``kind``:
      - ``"replay"`` — case A: ``response`` is the stored receipt
        response, returned verbatim (same audit_log_id, same
        published_version).
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
        """Design §4 steps 3–4. Read-only inside ONE transaction.

        A conflict raises inside the ``transaction()`` context manager,
        which rolls the BEGIN back — structurally guaranteeing "zero
        business change" (§5 case B) even though this probe writes
        nothing.
        """
        fingerprint = request_fingerprint(request)
        db = self._database
        with db.transaction():
            row = db.conn.execute(
                "SELECT ir.request_fingerprint AS fingerprint,"
                "       pr.plan_id AS original_plan_id,"
                "       pr.plan_version AS original_plan_version,"
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
                    row["original_plan_id"],
                    stored.get("status"),
                    original_plan_version=row["original_plan_version"],
                )
            # Case A: replay the stored response verbatim. Re-canonicalised
            # bytes equal response_json exactly — receipt is the single
            # success payload (design §3 design fact 2).
            return ProbeResult("replay", json.loads(row["response_json"]))


__all__ = [
    "TOOL_NAME",
    "REQUEST_FIELDS",
    "ProbeResult",
    "PublisherService",
    "PublishIdempotencyConflictError",
    "request_fingerprint",
]
