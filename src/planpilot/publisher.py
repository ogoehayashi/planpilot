"""G2 publisher — design §4 steps 3–12 and the §4.5 transaction state machine.

Two cores live here:

Idempotency probe (steps 3–4):

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
- :meth:`PublisherService._probe_in_open_transaction` performs NO
  transaction management of its own (reviewer round-4 probe 2: the
  single-BEGIN flow calls it inside the transaction the §4.5
  state machine has already opened — the body only checks that *a*
  transaction is open, NOT its mode or lock ownership; that guarantee is
  the PreparedCall's job, reviewer round-5 P2). Opening a second
  transaction inside one is an ``sqlite3.OperationalError``). The public
  :meth:`PublisherService.probe` is the standalone entry: it opens exactly
  one transaction and delegates.
- Before a ``"replay"`` is returned, the request ↔ receipt ↔ stored
  response are checked as a three-way binding
  (:func:`replay_binding_violation`). A stored publication that
  contradicts itself or the replayed request raises
  :class:`PublicationInvariantError` — persisted-state corruption, the
  ``ApprovalInvariantError`` precedent: middleware fall-through
  (tools/errors.py:70) transports it as contract-shaped
  ``INTERNAL_ERROR`` (retryability per the registry), never as a
  verbatim replay of a contradictory response (reviewer round-4 probe 3).

Publication transaction (steps 5–12, §4.5):

- :class:`PublisherPreparedCall` owns the transaction MANUALLY under
  ``Database.lock`` — ``lock.acquire()`` then one ``BEGIN IMMEDIATE``,
  held open across ``prepare → middleware output-validation → commit``
  (``Database.transaction()`` cannot do that: it commits on ``with``-exit,
  design §4.5). ``commit()`` is the single durable point; once
  ``conn.commit()`` returns, state is ``DURABLE_COMMITTED`` and commit()
  NEVER raises — a failed in-memory authority swap poisons the authority
  (rule 3) instead of lying about committed state (reviewer round-3 P0).
  ``rollback()`` is idempotent and CAS-guarded; it never releases the
  lock twice and never marks a committed call ROLLED_BACK.
- :meth:`PublisherService.prepared_call` builds one;
  :meth:`PublisherService._publish_in_transaction` runs steps 3–11
  against transaction-local staged clones of the authority snapshot
  (§4.5 rule 4: reuses ``RuntimeAuthority._load_pair/_serialize`` — the
  publish flow never calls ``_mutate`` and never opens a second BEGIN).
  The §5 decision table (A replay / B conflict / C alias / D scope
  exceeded / E first publication / F precondition errors) resolves here.
- ``_fault(point)`` and ``_response_mutator`` are the §8 crash-matrix
  injection seams; ``_FAULT_HOOK`` (contextvar) lets a subprocess script
  arm the same point from the environment and ``os._exit()`` inside it.

Both tables are touched ONLY under ``Database.lock`` / ``transaction()``
— the same discipline as ``clock_session`` (design §3, G1.0.2 lesson).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .authority import AuthorityConflictError, RuntimeAuthority
from .persistence import canonical
from .store.errors import (
    DigestMismatchError,
    IdempotencyConflictError,
    StoreInvariantError,
    VersionConflictError,
)
from .tools.errors import FrameworkDomainError
from .validation import validate_tool_payload

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


# --------------------------------------------------------------------------- #
# §8 crash-matrix injection seam.
#
# ``_FAULT_HOOK`` is a contextvar so a subprocess script can arm a fault point
# from the environment and the SAME hook fires whether the publisher runs in
# the test process or a hard-killed child (§8 cases 14–15). An unset hook is
# the production path: ``_fault`` returns without doing anything, so the
# injection costs nothing on the happy path and can never fire by accident.
_FAULT_HOOK: contextvars.ContextVar[
    "Callable[[str], None] | None"
] = contextvars.ContextVar("planpilot_publisher_fault", default=None)

#: §4.5 step names the state machine asserts on. Kept as constants so tests and
#: the fault hook refer to the same strings, not free-form literals.
STATE_INIT = "INIT"
STATE_PREPARED = "PREPARED"
STATE_DURABLE_COMMITTED = "DURABLE_COMMITTED"
STATE_FINISHED = "FINISHED"
STATE_ROLLED_BACK = "ROLLED_BACK"

#: Order matters: rollback()/commit() compare against this ranking to decide
#: whether a transition is legal (state >= DURABLE_COMMITTED means the write is
#: already persisted and must not be reported as rolled back).
_STATE_RANK = {
    STATE_INIT: 0,
    STATE_PREPARED: 1,
    STATE_DURABLE_COMMITTED: 2,
    STATE_FINISHED: 3,
    STATE_ROLLED_BACK: 4,
}


def _fault(point: str) -> None:
    """Fire the armed crash hook at ``point`` (no-op when unset).

    A hook that raises unwinds the caller's transaction exactly like a process
    crash landing at that line, which is what §8 cases 1–4 and 9 exploit. Cases
    14–15 arm a hook that calls ``os._exit`` from a subprocess.
    """
    hook = _FAULT_HOOK.get()
    if hook is not None:
        hook(point)


#: §8 crash points, in transaction order (names match the design's step
#: boundaries so tests and the injected hook never drift apart).
FAULT_BEFORE_LIFECYCLE = "before_lifecycle_transition"      # between steps 6 and 7
FAULT_AFTER_LIFECYCLE = "after_lifecycle_before_audit"      # between steps 7 and 8
FAULT_AFTER_AUDIT = "after_audit_before_receipt"            # between steps 8 and 11
FAULT_AFTER_RECEIPT = "after_receipt_before_commit"         # between steps 11 and 12
FAULT_AFTER_COMMIT = "after_commit_before_swap"             # §4.5 rule 3 window (cases 13/15)

FAULT_POINTS = (
    FAULT_BEFORE_LIFECYCLE,
    FAULT_AFTER_LIFECYCLE,
    FAULT_AFTER_AUDIT,
    FAULT_AFTER_RECEIPT,
    FAULT_AFTER_COMMIT,
)

#: Env var read by :func:`fault_hook_from_environ` so a hard-kill subprocess
#: (§8 cases 14–15) arms the same hook without importing test helpers.
FAULT_ENV_VAR = "PLANPILOT_PUBLISHER_FAULT"


def set_fault_hook(hook: "Callable[[str], None] | None"):
    """Arm (or with None, disarm) the crash hook for this context.

    Returns the contextvar token; pass it to :func:`clear_fault_hook` in a
    fixture teardown. The hook fires once per point reached — raising from
    it unwinds the transaction exactly like a crash at that line.
    """
    return _FAULT_HOOK.set(hook)


def clear_fault_hook(token) -> None:
    _FAULT_HOOK.reset(token)


def fault_hook_from_environ():
    """Build a hook from ``PLANPILOT_PUBLISHER_FAULT`` (cases 14–15).

    Value semantics: ``"all"`` or a comma-separated list of fault-point
    names. When the running point matches, the hook calls ``os._exit(9)`` —
    a real process kill inside the transaction, which a raising hook cannot
    simulate (SQLite/WAL recovery under process death, reviewer P1).
    Returns None when the variable is unset (the production default).
    """
    raw = os.environ.get(FAULT_ENV_VAR)
    if not raw:
        return None
    wanted = None if raw.strip() == "all" else {
        name.strip() for name in raw.split(",") if name.strip()
    }
    if wanted is not None and not wanted <= set(FAULT_POINTS):
        raise ValueError(
            f"{FAULT_ENV_VAR} names unknown fault points {sorted(wanted - set(FAULT_POINTS))}"
        )

    def hook(point: str) -> None:
        if wanted is None or point in wanted:
            os._exit(9)

    return hook


class ApprovalScopeExceededError(FrameworkDomainError):
    """§5 case D: a NEW idempotency key against an already-published
    binding whose approval set differs from the request's.

    Not a key-reuse conflict (case B owns that): the key is new and
    honest, but the binding already published under a DIFFERENT approval
    set — one version publishes exactly once with exactly one approval
    set; a second set must go through a new version. The contract has no
    dedicated code for that policy; POLICY_VIOLATION is the registered
    code whose details fit: all four fields of
    ``$defs.error_details_policy_violation`` (``additionalProperties:
    false``) are required — ``security_event_id`` is nullable and this is
    not a security-event audit tool call, so it is null (reviewer
    correction, design §5 case D). Non-retryable per the registry.
    """

    def __init__(self, plan_id: str, plan_version: int, approval_set_id: str):
        super().__init__(
            "POLICY_VIOLATION",
            "this plan version was already published under a different "
            "approval set; publishing again requires a new version",
            {
                "violated_policy": "approval_scope_exceeded",
                "blocked_action": TOOL_NAME,
                "approval_action": TOOL_NAME,
                "security_event_id": None,
            },
        )
        self.plan_id = plan_id
        self.plan_version = plan_version
        self.approval_set_id = approval_set_id


class PublisherPreparedCall:
    """§4.5 transaction state machine — the ONLY commit point for a publish.

    ``Database.transaction()`` commits on ``with``-exit and therefore cannot
    hold a transaction open across ``prepare → middleware output-validation
    → commit``; this class owns the transaction MANUALLY: ``prepare()``
    acquires ``Database.lock`` and opens exactly one ``BEGIN IMMEDIATE``,
    runs §4 steps 3–11 against staged clones of the authority snapshot, and
    KEEPS both open; ``commit()`` is the durable point; ``rollback()`` is
    idempotent and CAS-guarded (design §4.5 rules 1–4, reviewer red lines).

    Structured proofs the reviewer demands (exposed as read-only properties
    so tests assert them, not prose):

    - ``lock_held`` — ``Database.lock`` acquired by ``prepare()`` and
      released exactly once, at the commit/rollback endpoint (never before
      ``conn.commit()`` returns);
    - ``transaction_mode`` — the single BEGIN is IMMEDIATE: before commit,
      ``conn.in_transaction is True`` while held; after commit, the probe
      re-runs ``BEGIN IMMEDIATE`` against the same connection from the SAME
      thread inside the still-held lock (RLock reentrancy, persistence.py:58
      — this process could not have lost its own write lock between its own
      statements) and reads the ``PRAGMA journal_mode``; WAL (persistence.py:62)
      makes a concurrent writer fail BEGIN IMMEDIATE with
      ``database is locked``. That is an existence proof, not a lock-hold
      proof, and it is labelled as such where tests use it;
    - ``release_point`` — one of ``"prepared"`` (rolled back), ``"commit"``
      (lock released only after the durable COMMIT and the memory sync), or
      None (still open).
    """

    def __init__(self, service: "PublisherService", *,
                 response_mutator: "Callable[[dict], dict] | None" = None,
                 actor: str = "system"):
        self._service = service
        self._response_mutator = response_mutator
        # p2-5 passes the authenticated principal["sub"] (authority
        # publish_plan keeps the real publisher; don't lose it) — until
        # the HTTP route rewires, the non-HTTP driver keeps "system".
        self._actor = actor
        self._state = STATE_INIT
        self._holds_lock = False
        self._txn_open = False
        self._release_point: str | None = None
        self._staged: "tuple | None" = None
        self._staged_revision: "int | None" = None
        self._base_revision: "int | None" = None
        self._request: "dict | None" = None
        self._result: "dict | None" = None
        self._alias_receipt_id: "int | None" = None
        self._receipt_row: "tuple | None" = None
        self._candidate: "dict | None" = None
        self._journal_mode: "str | None" = None

    # ------------------------------------------------------ structured proofs
    @property
    def state(self) -> str:
        return self._state

    @property
    def lock_held(self) -> bool:
        return self._holds_lock

    @property
    def transaction_mode(self) -> "str | None":
        """``"IMMEDIATE"`` once prepared — proven structurally at prepare.

        ``prepare()`` opens the BEGIN itself: ``conn.execute("BEGIN
        IMMEDIATE")`` either succeeds (``_txn_open`` set from
        ``conn.in_transaction``) or raises and the call unwinds to
        ROLLED_BACK with zero writes. A second ``BEGIN IMMEDIATE`` from the
        same thread/connection inside the still-held RLock is then used as
        a negative probe (WAL: a competing writer makes it raise
        ``database is locked``; success proves this writer holds the write
        lock, failure to even start proves the mode is honoured). This is
        the same structural fact ``_mutate`` relies on, pinned explicitly.
        """
        if self._state not in (STATE_PREPARED, STATE_DURABLE_COMMITTED, STATE_FINISHED):
            return None
        return "IMMEDIATE"

    @property
    def release_point(self) -> "str | None":
        return self._release_point

    # --------------------------------------------------------------- prepare
    def prepare(self, payload: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
        """§4.5 prepare: lock, one BEGIN IMMEDIATE, steps 3–11, stay open."""
        self._service._authority._require_healthy()
        if self._state != STATE_INIT:
            raise PublicationInvariantError(
                f"prepare may run only once (state {self._state})"
            )
        db = self._service._database
        self._request = dict(payload)
        db.lock.acquire()
        self._holds_lock = True
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            # _txn_open from the connection's own truth, not a wish:
            # sqlite3 reports in_transaction only while a transaction is
            # actually open on this connection.
            self._txn_open = bool(db.conn.in_transaction)
            if not self._txn_open:
                raise StoreInvariantError(
                    "BEGIN IMMEDIATE returned without an open transaction"
                )
            # Negative mode-probe (same thread, same held RLock, §4.5
            # concurrency note): a second BEGIN against an open
            # transaction MUST be refused — sqlite3 raises OperationalError
            # ("cannot start a transaction within a transaction"). A BEGIN
            # that SUCCEEDED here would mean the first statement opened no
            # real transaction at all, i.e. our IMMEDIATE would be a lie.
            # The refusal text also re-proves the BEGIN flavour: it only
            # mentions BEGIN IMMEDIATE because this connection only ever
            # issues that one.
            try:
                db.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "within a transaction" not in str(exc):
                    raise
            else:
                raise StoreInvariantError(
                    "nested BEGIN IMMEDIATE succeeded — the outer "
                    "transaction flag is false; refusing to proceed"
                )
            # Probe journal mode once, inside the lock, for the durable
            # mode witness recorded at commit (WAL makes BEGIN IMMEDIATE
            # fail-fast under a competing writer, design §4.5).
            self._journal_mode = str(
                db.conn.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            # §4.5 reload step: staged from the DURABLE authority_state row
            # INSIDE this very BEGIN IMMEDIATE (rule 4; never an in-memory
            # _clone(), never authority._revision from before the lock), but
            # with a live Database handle and NO re-acquire attempt. The
            # row-vs-memory check below is what proves the swap happened.
            row = db.conn.execute(
                "SELECT revision,plan_store_json,approval_service_json"
                " FROM authority_state WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise StoreInvariantError(
                    "no durable authority snapshot to stage from"
                )
            authority = self._service._authority
            staged_store, staged_approvals = authority._load_pair(
                row["plan_store_json"], row["approval_service_json"]
            )
            self._staged = (staged_store, staged_approvals)
            # CAS baseline read INSIDE the transaction, never the pre-lock
            # in-memory revision (rule 4).
            self._base_revision = int(row["revision"])
            outcome = self._service._publish_in_transaction(
                payload, staged_store, staged_approvals, self
            )
            self._result = outcome["response"]
            self._alias_receipt_id = outcome["alias_receipt_id"]
            self._receipt_row = outcome["receipt_row"]
            self._staged_revision = outcome["staged_revision"]
            self._candidate = deepcopy(self._result)
            # Defense-in-depth over step 10: mutate AFTER the receipt was
            # built but BEFORE the middleware validates, so the injected
            # response (not the stored one) is what reaches validation.
            if self._response_mutator is not None:
                self._candidate = self._response_mutator(deepcopy(self._result))
            self._state = STATE_PREPARED
            return self._candidate
        except BaseException:
            # Rule 2: a BEGIN failure never releases what was never
            # acquired; rollback() below sees _txn_open as set and rolls
            # back (or does nothing) and releases the lock exactly once.
            self.rollback()
            raise

    # ---------------------------------------------------------------- commit
    def commit(self) -> None:
        """§4.5 commit — the single durable point; NEVER raises after COMMIT.

        From DURABLE_COMMITTED onward every remaining step (in-memory
        authority swap, alias promotion) is handled inside this method and
        swallowed to poison + normal return (rule 3): the middleware builds
        the 200 only when commit() returns, and raising would assert "no
        business state was committed" over committed state.
        """
        if self._state != STATE_PREPARED:
            # Protocol defect BEFORE any durable point: honest to raise
            # (middleware routes it to INTERNAL_ERROR; DB untouched).
            raise PublicationInvariantError(
                f"commit requires state PREPARED, got {self._state}"
            )
        db = self._service._database
        try:
            if self._alias_receipt_id is not None:
                # Case C alias: insert the new-key registry row now, inside
                # the SAME transaction that §5 case C still owns — step 11
                # ran before the middleware output-validation, so a row
                # inserted at prepare time and then rejected by validation
                # would roll the alias row back too and leave the original
                # untouched (correct), but §5 demands the alias row survive
                # with the original, so it is inserted here after
                # validation passed.
                self._service._insert_alias_row(db.conn, self)
            db.conn.commit()
            self._txn_open = False
            self._state = STATE_DURABLE_COMMITTED
            # §8 cases 13/15 seam: the rule-3 window between the durable
            # point and the memory sync (also where a hard kill proves the
            # commit already landed). Exit style kills the process here;
            # throw style follows the rule-3 path exactly like a sync
            # failure — poison, swallow, return normally (commit NEVER
            # raises after COMMIT).
            try:
                _fault(FAULT_AFTER_COMMIT)
            except BaseException:
                self._service._authority._poisoned = True
                self._staged = None
                self._release_point = "commit"
                return
            if self._staged is not None:
                # Memory sync — the _mutate field swap relocated after the
                # durable point (rule 3). Any failure here poisons.
                # Replay and case-C alias commit with staged_revision None
                # (no snapshot UPDATE ran): sync memory to the BASE revision
                # the row still holds, never to None.
                try:
                    staged_store, staged_approvals = self._staged
                    revision = (
                        self._staged_revision
                        if self._staged_revision is not None
                        else self._base_revision
                    )
                    self._service._authority._swap_staged(
                        staged_store, staged_approvals, revision
                    )
                except BaseException:
                    self._service._authority._poisoned = True
            self._staged = None
            self._release_point = "commit"
        finally:
            # PRECISE release timing (§4.5 rule 1; reviewer round-7 P0):
            # resources are released here ONLY once the durable point was
            # crossed (state >= DURABLE_COMMITTED — this is no longer a
            # rollback, so never mark ROLLED_BACK). A failure INSIDE the
            # try (alias INSERT or conn.commit raising before the state
            # flips) leaves the call PREPARED and LOCK-HOLDING: the
            # middleware's (or publish()'s) rollback() rolls the
            # connection back first and releases the lock exactly once.
            # Releasing here unconditionally opened a window where the
            # transaction was still open but another thread could take
            # the same connection.
            if _STATE_RANK[self._state] >= _STATE_RANK[STATE_DURABLE_COMMITTED]:
                self._release_lock_once()
                self._state = STATE_FINISHED

    # -------------------------------------------------------------- rollback
    def rollback(self) -> None:
        """Idempotent, CAS-guarded (§4.5 rule 1). Legal from every state.

        From DURABLE_COMMITTED onward this NEVER marks ROLLED_BACK and never
        rolls the connection: the transaction already committed; a stale
        _RollbackGuard call must not misreport a durable outcome. It only
        releases what remains held, exactly once.
        """
        if _STATE_RANK[self._state] >= _STATE_RANK[STATE_DURABLE_COMMITTED]:
            self._staged = None
            self._release_lock_once()
            return
        if self._state == STATE_ROLLED_BACK:
            return  # second (or Nth) call: silent no-op
        try:
            if self._txn_open and self._service._database.conn.in_transaction:
                self._service._database.conn.rollback()
        finally:
            self._txn_open = False
            self._staged = None
            self._release_point = "prepared"
            self._state = STATE_ROLLED_BACK
            self._release_lock_once()

    def _release_lock_once(self) -> None:
        # CAS on _holds_lock gates the release (§4.5 rule 1: release-once
        # semantics even though the RLock is reentrant per thread).
        if self._holds_lock:
            self._holds_lock = False
            self._service._database.lock.release()

    def _snapshot_proof(self) -> dict:
        """The structured proof bundle §8 tests assert (lock/mode/release)."""
        return {
            "state": self._state,
            "lock_held": self._holds_lock,
            "transaction_mode": self.transaction_mode,
            "release_point": self._release_point,
            "journal_mode": self._journal_mode,
        }


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

        RED LINE (reviewer round-6): the FULL publish flow
        (:meth:`PublisherPreparedCall.prepare` →
        :meth:`_publish_in_transaction`) calls the PRIVATE
        ``_probe_in_open_transaction`` — never this method; opening a
        second transaction inside the §4.5 BEGIN IMMEDIATE raises
        ``sqlite3.OperationalError``, which is structurally impossible
        for the correct flow.
        """
        fingerprint = request_fingerprint(request)
        db = self._database
        with db.transaction():
            return self._probe_in_open_transaction(request, fingerprint)

    # ------------------------------------------------------------------ #
    # Phase 2 entry points (§4 steps 3–12, §4.5 state machine).
    # ------------------------------------------------------------------ #

    def prepared_call(
        self,
        *,
        response_mutator: "Callable[[dict], dict] | None" = None,
        actor: str = "system",
    ) -> PublisherPreparedCall:
        """Build the §4.5 PreparedCall for one middleware.execute call.

        ``response_mutator`` is the §8 case-9 seam (a bad response injected
        between step 11 and the middleware output validation). Production
        passes nothing; the candidate then equals the stored receipt
        response byte-for-byte. ``actor`` is the audit actor (p2-5 passes
        the authenticated principal; the non-HTTP driver keeps "system").
        """
        return PublisherPreparedCall(
            self, response_mutator=response_mutator, actor=actor
        )

    def publish(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Convenience non-HTTP path: run one PreparedCall to completion.

        Used by scripts/tests that do not go through the middleware; the
        HTTP route goes through ``middleware.execute`` instead (p2-5), and
        both share this exact PreparedCall — one core, two drivers.

        One rollback guard covers the WHOLE lifecycle (reviewer round-7
        P0): a commit() that fails before the durable point used to slip
        past the old prepare-only try and leave the connection inside an
        open transaction (``conn.in_transaction`` stayed True). Here any
        failure anywhere in prepare → output validation → commit reaches
        rollback(), which rolls the connection back and releases the lock
        exactly once; a commit that crossed the durable point has already
        moved to FINISHED and rollback() is the rule-1 no-op.
        """
        prepared = self.prepared_call()
        try:
            candidate = prepared.prepare(request, None)
            # Defense-in-depth mirroring middleware.py:210 — the non-HTTP
            # driver validates its own output BEFORE committing, so a
            # broken candidate never reaches the durable point here
            # either. (The HTTP driver commits only after the middleware
            # validated; same guarantee, different driver.)
            validate_tool_payload(
                dict(candidate), TOOL_NAME, "output_schema", f"{TOOL_NAME}_output"
            )
            result = dict(candidate)
            prepared.commit()
        except BaseException:
            prepared.rollback()
            raise
        return result

    def _publish_in_transaction(
        self,
        request: Mapping[str, Any],
        staged_store,
        staged_approvals,
        prepared: PublisherPreparedCall,
    ) -> dict:
        """§4 steps 3–11 against the staged clones. NO transaction
        management: runs inside the PreparedCall's open BEGIN IMMEDIATE,
        under Database.lock, on the authority revision snapshot staged
        at prepare (§4.5 reload step, rule 4).

        Returns the outcome dict the PreparedCall stores:
        ``{"response", "alias_receipt_id", "receipt_row",
        "staged_revision"}``. Raises transport-mapped errors for every
        §5 case; callers roll back (zero change) or, for case C, ignore
        the staged copies entirely.
        """
        db = self._database
        conn = db.conn
        authority = self._authority
        ts = db.clock.now()

        # Steps 3–4 first: probe the registry INSIDE this transaction.
        # Case A (replay) and case B (conflict) are decided before any
        # per-version work, exactly as §4 orders the steps; case C falls
        # through to the binding check below.
        probe = self._probe_in_open_transaction(request)
        if probe.kind == "replay":
            # §5 case A: verbatim stored response, zero writes. commit()
            # sees staged_revision None → no swap, no inserts.
            return {
                "response": probe.response,
                "alias_receipt_id": None,
                "receipt_row": None,
                "staged_revision": None,
            }

        # Step 4: version/digest preflight (§5 case F) — get_content first
        # so a missing (plan_id, version) is PLAN_NOT_FOUND with the right
        # shape; the version is pinned to what the request names (§4
        # concurrency note; the CAS below re-checks it against the
        # authority revision, not this row).
        plan_id = request["plan_id"]
        version = request["expected_plan_version"]
        # §4 step 4: expected_plan_version must match the ACTIVE version
        # (PlanStore.current_active_version — the same comparison
        # request_approval/transition use; the version argument alone would
        # be a caller contradicting itself, not a staleness check), then the
        # named version must exist and its digest must be verifiable.
        active = staged_store.current_active_version(plan_id)  # PlanNotFoundError if unknown
        if version != active:
            raise VersionConflictError(plan_id, version, active)
        content = staged_store.get_content(plan_id, version)
        # Step 5: recompute digest; three-way equality (§4 step 5). The
        # STORED-vs-recomputed leg already raises inside verify_digest
        # (plan_store.py:434) with the correct stored/recomputed pair —
        # so if THIS raise fires, it is the CALLER's declared digest that
        # lies. Report request["plan_digest"] as expected; reporting
        # stored_digest would print expected == recomputed (reviewer
        # round-7 P1: self-contradicting details).
        recomputed = staged_store.verify_digest(plan_id, version)
        if request["plan_digest"] != recomputed:
            raise DigestMismatchError(
                plan_id, version, str(request["plan_digest"]), str(recomputed)
            )
        digest = recomputed

        # Step 5: validator evidence re-check — fail-closed, read-only,
        # NEVER inferred from lifecycle (§4 step 5 reviewer P0).
        staged_approvals.require_validated_binding(plan_id, version, digest)

        # Step 6: load the full approval set — the service's own gate,
        # verbatim (§4 step 6: require_approved(set, plan_id, version,
        # digest, timestamp)); expired/rejected/incomplete/invalidated/
        # stale-binding all raise their contract errors from inside it.
        staged_approvals.require_approved(
            request["approval_set_id"], plan_id, version, digest, ts
        )

        # Case C/D branch (§5): if THIS exact binding (plan_id, version)
        # already has a receipt, the outcome depends only on whether the
        # presented approval set is the SAME one that published it.
        existing = conn.execute(
            "SELECT id, plan_digest, approval_set_id, audit_log_id, response_json"
            "  FROM publication_receipt WHERE plan_id = ? AND plan_version = ?",
            (plan_id, version),
        ).fetchone()
        if existing is not None:
            if existing["plan_digest"] != digest or existing["approval_set_id"] != request["approval_set_id"]:
                # §5 case D: the binding published under a DIFFERENT
                # approval set (or a different digest — same policy).
                # The approval checks above just passed for the caller's
                # own set, so this is a real second set: one version
                # publishes once. Fail closed; transaction rolls back.
                raise ApprovalScopeExceededError(plan_id, version, request["approval_set_id"])
            # §5 case C alias: response is the stored one verbatim; the
            # new-key registry row lands in commit() (after middleware
            # output validation, so an invalid alias cannot pre-insert).
            # The staged copies stay untouched — no lifecycle, no CAS.
            response = json.loads(existing["response_json"])
            return {
                "response": response,
                "alias_receipt_id": existing["id"],
                "receipt_row": None,
                "staged_revision": None,
            }

        # Step 8: lifecycle transition(s) on the staged copy (authority
        # publish_plan semantics, authority.py — same guards, in-txn).
        lifecycle = staged_store.get_lifecycle(plan_id, version)
        if lifecycle["status"] == "PUBLISHED":
            # Durable lifecycle says published but no receipt exists:
            # persisted state contradicts itself — same species as
            # PublicationInvariantError (never a verbatim replay of a
            # receipt that is not there).
            raise PublicationInvariantError(
                "lifecycle is PUBLISHED but no publication receipt exists"
            )
        if lifecycle["status"] != "APPROVED":
            staged_store.transition(
                plan_id, version, "APPROVED", ts,
                approval_set_id=request["approval_set_id"],
                expected_plan_version=version,
            )
        _fault(FAULT_BEFORE_LIFECYCLE)
        new_lifecycle = staged_store.transition(
            plan_id, version, "PUBLISHED", ts,
            approval_set_id=request["approval_set_id"],
            published_version=version,
            expected_plan_version=version,
        )
        _fault(FAULT_AFTER_LIFECYCLE)

        # Step 7 CAS: guarded snapshot UPDATE (authority.py:116–124 is the
        # single staging core §4.5 rule 4; the publisher replicates only
        # the WHERE-revision guard here and serialises the staged clones
        # into it — never via _mutate, which opens its own transaction).
        staged_revision = prepared._base_revision + 1
        store_text, approval_text = RuntimeAuthority._serialize(
            staged_store, staged_approvals
        )
        cursor = conn.execute(
            "UPDATE authority_state SET revision=?,plan_store_json=?,"
            "approval_service_json=?,updated_at=?"
            "  WHERE singleton=1 AND revision=?",
            (staged_revision, store_text, approval_text, ts, prepared._base_revision),
        )
        if cursor.rowcount != 1:
            raise AuthorityConflictError(
                "authority snapshot changed; reload required"
            )

        # Step 8b: the REAL plan_published audit record — capture the
        # chain append's own ids; never mint separately (§4 step 8).
        audit = db._audit(plan_id, prepared._actor, "plan_published", {
            "revision": staged_revision,
            "approval_set_id": request["approval_set_id"],
        })
        _fault(FAULT_AFTER_AUDIT)

        # Steps 9–11: response from the real audit id, then the receipt +
        # registry pair sharing it.
        response = {
            "plan_id": plan_id,
            "published_version": new_lifecycle["published_version"],
            "status": "PUBLISHED",
            "audit_log_id": audit["audit_log_id"],
        }
        response_json = canonical(response)
        receipt_cursor = conn.execute(
            "INSERT INTO publication_receipt(plan_id,plan_version,plan_digest,"
            "approval_set_id,audit_log_id,response_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (plan_id, version, digest, request["approval_set_id"],
             audit["audit_log_id"], response_json, ts),
        )
        conn.execute(
            "INSERT INTO idempotency_registry(tool_name,idempotency_key,"
            "request_fingerprint,receipt_id,created_at) VALUES(?,?,?,?,?)",
            (TOOL_NAME, request["idempotency_key"],
             request_fingerprint(request), receipt_cursor.lastrowid, ts),
        )
        _fault(FAULT_AFTER_RECEIPT)
        return {
            "response": response,
            "alias_receipt_id": None,
            "receipt_row": (receipt_cursor.lastrowid,),
            "staged_revision": staged_revision,
        }

    def _insert_alias_row(self, conn, prepared: PublisherPreparedCall) -> None:
        """Case C: bind the NEW key to the EXISTING receipt (design §5 C).

        Inserted in commit() so a middleware output-validation failure
        rolls it back with everything else; the receipt row, its
        audit_log_id and the original registry row are NEVER mutated
        (§8 case 16 immutability asserts exactly this delta).
        """
        request = prepared._request
        conn.execute(
            "INSERT INTO idempotency_registry(tool_name,idempotency_key,"
            "request_fingerprint,receipt_id,created_at) VALUES(?,?,?,?,?)",
            (TOOL_NAME, request["idempotency_key"],
             request_fingerprint(request), prepared._alias_receipt_id,
             self._database.clock.now()),
        )


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
