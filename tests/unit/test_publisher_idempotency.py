"""G2 tasks 1.2–1.4 (hardened per reviewer round-4) — fingerprint + probe.

1.2: ``request_fingerprint`` = sha256 over ``canonical()`` of the exact
five contract fields (same canonical the audit chain uses). Key-order
independence and any-field sensitivity are pinned here, plus the
fail-closed guard against malformed requests. Fixtures use contract-
legal digests: publish_plan input_schema pins plan_digest to
``^[a-f0-9]{64}$`` — the old ``"sha256:"+64hex`` form is what the real
contract validator would reject (reviewer probe 1).
1.3: ``PublisherService._probe_in_open_transaction`` = design §4 steps
3–4 body with NO transaction management of its own; the public
``probe()`` wraps it in exactly one transaction. Calling the public
probe inside an open transaction must fail fast (reviewer probe 2).
1.4: §5 cases A/B (§8 case 6/7 pre-wired on the probe-only path):
two barrier-synced connections racing the same key — same payload →
both replay identically, exactly one registry row; different payload →
one wins, the other conflicts, and nothing on the losing side changed.
"Unchanged" is asserted as an identical NORMALISED LOGICAL STATE — a
canonical dump of every table — not as byte-identical files: SQLite
may lay pages out differently for logically identical databases.
Replays additionally require the request↔receipt↔response three-way
binding to hold (reviewer probe 3): a self-contradicting stored
publication raises PublicationInvariantError, never a verbatim 200.
"""
import hashlib
import json
import sqlite3
import threading
import time
import uuid

import pytest

from planpilot.persistence import Database, canonical
from planpilot.publisher import (
    REQUEST_FIELDS,
    PublicationInvariantError,
    PublishIdempotencyConflictError,
    PublisherService,
    replay_binding_violation,
    request_fingerprint,
)
from planpilot.store.errors import IdempotencyConflictError, StoreInvariantError

KEY = "idem-4471e5c29f084cd3b7fe682a7a142df4"
# Contract-legal 64-char lowercase hex (publish_plan input_schema
# plan_digest pattern ^[a-f0-9]{64}$).
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
DIGEST_E = "e" * 64
REQUEST = {
    "plan_id": "plan_7f3a91",
    "expected_plan_version": 3,
    "plan_digest": DIGEST_A,
    "approval_set_id": "apr_12",
    "idempotency_key": KEY,
}
# A truthful stored publication: receipt binding plan_version=3 is the
# SAME version the response reports as published (design §4 step 11:
# both rows share one transaction — plan_version, digest, approval set,
# audit id and the canonical response must tell one story).
STORED_RESPONSE = {
    "status": "PUBLISHED",
    "plan_id": "plan_7f3a91",
    "published_version": 3,
    "audit_log_id": "log_1",
}

REGISTRY_COLS = ("tool_name", "idempotency_key", "request_fingerprint",
                 "receipt_id", "created_at")
RECEIPT_COLS = ("plan_id", "plan_version", "plan_digest", "approval_set_id",
                "audit_log_id", "response_json", "created_at")


def seed(db, response=STORED_RESPONSE, fingerprint=None, key=KEY,
         audit_log_id="log_1", plan_id="plan_7f3a91", plan_version=3,
         plan_digest=DIGEST_A, approval_set_id="apr_12"):
    """Write one receipt + one registry row through the production
    constraint surface (plain INSERTs under transaction())."""
    with db.transaction():
        c = db.conn
        c.execute(
            f"INSERT INTO publication_receipt ({', '.join(RECEIPT_COLS)}) "
            f"VALUES ({', '.join('?' * len(RECEIPT_COLS))})",
            (plan_id, plan_version, plan_digest,
             approval_set_id, audit_log_id,
             json.dumps(response, sort_keys=True),
             "2026-09-14T09:00:00+08:00"))
        c.execute(
            f"INSERT INTO idempotency_registry ({', '.join(REGISTRY_COLS)}) "
            f"VALUES ({', '.join('?' * len(REGISTRY_COLS))})",
            ("publish_plan", key,
             fingerprint or request_fingerprint(REQUEST), 1,
             "2026-09-14T09:00:00+08:00"))


def db_digest(path):
    """Normalised logical-state identity of the database.

    Canonical-dumps every user table (rows stringified and sorted) and
    hashes the result. This is deliberately NOT the file's bytes: two
    logically identical SQLite files can differ byte-wise (page layout,
    freelist, WAL leftovers), so a byte claim would be both false and
    unportable. What IS claimed and needed: every row of every table
    unchanged, which is exactly the "zero business change" invariant
    (design §5 case B).
    """
    conn = sqlite3.connect(path)
    try:
        frames = []
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            rows = conn.execute(f"SELECT * FROM {name}").fetchall()
            frames.append((name, [list(map(str, r)) for r in sorted(
                rows, key=lambda r: str(r))]))
        return hashlib.sha256(canonical(frames).encode("utf-8")).hexdigest()
    finally:
        conn.close()


# ---------------------------------------------------------------- 1.2


def test_fingerprint_is_sha256_of_canonical_five_field_request():
    exact = {field: REQUEST[field] for field in REQUEST_FIELDS}
    assert request_fingerprint(REQUEST) == hashlib.sha256(
        canonical(exact).encode("utf-8")).hexdigest()


def test_fingerprint_ignores_key_order():
    reordered = dict(reversed(list(REQUEST.items())))
    assert list(reordered) != list(REQUEST)  # insertion order really differs
    assert request_fingerprint(reordered) == request_fingerprint(REQUEST)


@pytest.mark.parametrize("field", REQUEST_FIELDS)
def test_fingerprint_changes_with_any_single_field(field):
    def bump(value):
        if isinstance(value, int):
            return value + 1
        return value + "f"  # still 64-char-hex-shaped or plain string change
    changed = {**REQUEST, field: bump(REQUEST[field])}
    assert request_fingerprint(changed) != request_fingerprint(REQUEST)


def test_fingerprint_covers_idempotency_key_too():
    """The key is part of the request side (design §3: 'key included'),
    so a same-binding request under a different key is a different
    fingerprint — Case C/D route by binding, never by replay match."""
    other = {**REQUEST, "idempotency_key": "z" * 32}
    assert request_fingerprint(other) != request_fingerprint(REQUEST)


@pytest.mark.parametrize("drop", REQUEST_FIELDS)
def test_missing_field_fails_closed_not_silently_hashed(drop):
    bad = {k: v for k, v in REQUEST.items() if k != drop}
    with pytest.raises(StoreInvariantError):
        request_fingerprint(bad)


def test_extra_field_fails_closed():
    with pytest.raises(StoreInvariantError):
        request_fingerprint({**REQUEST, "operator_note": "ship it"})


# ---------------------------------------------------------------- 1.3


def test_probe_miss_returns_first_and_writes_nothing(tmp_path):
    path = str(tmp_path / "t.db")
    db = Database(path)
    before = db_digest(path)
    result = PublisherService(db, None).probe(REQUEST)
    assert result.kind == "first" and result.response is None
    assert db_digest(path) == before          # logical state unchanged
    assert db.conn.execute(
        "SELECT COUNT(*) FROM idempotency_registry").fetchone()[0] == 0
    db.close()


def test_probe_hit_same_fingerprint_replays_stored_response(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    seed(db)
    result = PublisherService(db, None).probe(REQUEST)
    assert result.kind == "replay"
    # Verbatim: the dict re-serialises to the same canonical bytes as
    # the stored response_json — same audit_log_id, same published_version.
    assert json.dumps(result.response, sort_keys=True) == json.dumps(
        STORED_RESPONSE, sort_keys=True)
    db.close()


def test_conflict_details_match_contract_shape_and_retryability(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    seed(db)
    ps = PublisherService(db, None)
    with pytest.raises(PublishIdempotencyConflictError) as exc:
        ps.probe({**REQUEST, "plan_digest": DIGEST_B})
    err = exc.value
    # $defs.error_details_idempotency_conflict: exactly these three keys,
    # additionalProperties:false.
    assert err.to_error_details() == {
        "idempotency_key": KEY,
        "original_plan_id": "plan_7f3a91",
        "original_status": "PUBLISHED",
    }
    assert err.code == "IDEMPOTENCY_CONFLICT"
    assert err.retryable is False
    assert isinstance(err, IdempotencyConflictError)  # same code, no new one
    db.close()


def test_probe_never_touches_authority(tmp_path):
    """Phase 1 pin: the probe path must not read or write the runtime
    authority — a service built with authority=None proves it."""
    db = Database(str(tmp_path / "t.db"))
    seed(db)
    ps = PublisherService(db, None)
    assert ps.probe(REQUEST).kind == "replay"
    with pytest.raises(PublishIdempotencyConflictError):
        ps.probe({**REQUEST, "approval_set_id": "apr_99"})
    db.close()


# ------------------------------------------------- 1.3 hardening (round-4)


def test_probe_body_runs_inside_outer_transaction(tmp_path):
    """Reviewer probe 2: Phase 2 calls the body INSIDE its own BEGIN
    IMMEDIATE. It must neither open a second transaction nor touch the
    commit — here: an outer transaction with a write, the probe body,
    then ROLLBACK must erase the write and the probe still decides."""
    path = str(tmp_path / "t.db")
    db = Database(path)
    seed(db)
    db.close()
    db = Database(path)
    with db.lock:
        db.conn.execute("BEGIN IMMEDIATE")
        assert db.conn.in_transaction
        db.conn.execute(  # hypothetical Phase 2 write, same transaction
            "INSERT INTO idempotency_registry "
            f"({', '.join(REGISTRY_COLS)}) "
            f"VALUES ({', '.join('?' * len(REGISTRY_COLS))})",
            ("publish_plan", "idem-ghostkey-000000000000000000ff",
             "f" * 64, 1, "2026-09-14T09:00:00+08:00"))
        result = PublisherService(db, None)._probe_in_open_transaction(REQUEST)
        assert result.kind == "replay"
        db.conn.rollback()
    assert not db.conn.in_transaction
    assert db.conn.execute(
        "SELECT COUNT(*) FROM idempotency_registry").fetchone()[0] == 1
    db.close()


def test_probe_body_refuses_closed_transaction(tmp_path):
    """Caller-usage defect (no open BEGIN) crashes as StoreInvariantError
    instead of silently reading outside a transaction."""
    db = Database(str(tmp_path / "t.db"))
    seed(db)
    with pytest.raises(StoreInvariantError):
        PublisherService(db, None)._probe_in_open_transaction(REQUEST)
    db.close()


def test_public_probe_inside_open_transaction_fails_fast(tmp_path):
    """The public wrapper opens its own BEGIN — calling it from inside a
    transaction raises sqlite3.OperationalError (never a silent nested
    BEGIN). This is the exact failure reviewer probe 2 reproduced; the
    Phase 2 flow must use the body instead."""
    db = Database(str(tmp_path / "t.db"))
    seed(db)
    svc = PublisherService(db, None)
    with db.transaction():
        with pytest.raises(sqlite3.OperationalError):
            svc.probe(REQUEST)
    # transaction rolled back cleanly; the replay still works standalone
    assert svc.probe(REQUEST).kind == "replay"
    db.close()


# ------------------------------- 1.3 hardening: three-way replay binding


def test_binding_helper_accepts_consistent_triple():
    receipt = {"plan_id": "plan_7f3a91", "plan_version": 3,
               "plan_digest": DIGEST_A, "approval_set_id": "apr_12",
               "audit_log_id": "log_1"}
    assert replay_binding_violation(REQUEST, receipt,
                                    dict(STORED_RESPONSE)) is None


def test_binding_helper_flags_version_contradiction():
    receipt = {"plan_id": "plan_7f3a91", "plan_version": 3,
               "plan_digest": DIGEST_A, "approval_set_id": "apr_12",
               "audit_log_id": "log_1"}
    bad = {**STORED_RESPONSE, "published_version": 7}
    why = replay_binding_violation(REQUEST, receipt, bad)
    assert why is not None and "published_version" in why


@pytest.mark.parametrize("broken", [
    # response_json contradicts the receipt in each registered way
    {**STORED_RESPONSE, "published_version": 7},          # reviewer probe 3
    {**STORED_RESPONSE, "audit_log_id": "log_forged"},
    {**STORED_RESPONSE, "plan_id": "plan_forged"},
    {**STORED_RESPONSE, "status": "DRAFT"},               # output const lost
    {**STORED_RESPONSE, "operator_note": "extra"},        # additionalProperties
    "not-an-object",
])
def test_contradictory_stored_publication_refuses_replay(tmp_path, broken):
    """A fingerprint hit is NOT sufficient for a verbatim 200: the
    stored receipt and response must tell one story. Contradiction →
    PublicationInvariantError (never replayed, never a silent success)."""
    db = Database(str(tmp_path / "t.db"))
    seed(db, response=broken)
    svc = PublisherService(db, None)
    with pytest.raises(PublicationInvariantError):
        svc.probe(REQUEST)
    # and the standalone conflict path (different fingerprint) still works:
    db.close()


@pytest.mark.parametrize("column,value", [
    ("plan_digest", DIGEST_E),        # tampered AFTER commit under a real
    ("approval_set_id", "apr_evil"),  # (still matching) fingerprint
])
def test_receipt_tampering_refuses_replay(tmp_path, column, value):
    """Registry fingerprint matches the request byte-for-byte, but the
    receipt row was rewritten behind it — request↔receipt check catches
    what the fingerprint alone cannot."""
    db = Database(str(tmp_path / "t.db"))
    seed(db)
    with db.transaction():
        db.conn.execute(
            f"UPDATE publication_receipt SET {column} = ? WHERE id = 1",
            (value,))
    with pytest.raises(PublicationInvariantError):
        PublisherService(db, None).probe(REQUEST)
    db.close()


def test_binding_violation_surfaces_as_internal_error_via_middleware(tmp_path):
    """Reviewer probe 3, end of wire: PublicationInvariantError is NOT a
    StoreError, so tools.errors fall-through (adapt_exception,
    errors.py:70) transports it as the contract-shaped INTERNAL_ERROR —
    retryable per the registry — and never as a success replay."""
    from planpilot.tools.errors import validated_failure

    exc = PublicationInvariantError("response published_version 7 contradicts "
                                    "receipt plan_version 3")
    payload, wire = validated_failure("publish_plan", exc, str(uuid.uuid4()))
    assert payload["error_code"] == "INTERNAL_ERROR"
    assert payload["retryable"] is True  # registry says INTERNAL_ERROR=True
    assert set(payload["details"]) == {"diagnostic_class", "safe_detail"}
    assert payload["details"]["diagnostic_class"] == "PublicationInvariantError"
    assert b"published_version 7" not in wire  # no internal detail leaks out


def test_contradictory_replay_rolls_back_outer_transaction(tmp_path):
    """The raise happens inside the outer BEGIN, so a Phase 2-style
    transaction around the probe discards everything it had staged."""
    path = str(tmp_path / "t.db")
    db = Database(path)
    seed(db, response={**STORED_RESPONSE, "published_version": 7})
    db.close()
    before = db_digest(path)
    db2 = Database(path)
    with pytest.raises(PublicationInvariantError):
        with db2.transaction():               # one BEGIN IMMEDIATE, as §4.5
            db2.conn.execute(                 # hypothetical staged write…
                "INSERT INTO publication_receipt "
                f"({', '.join(RECEIPT_COLS)}) "
                f"VALUES ({', '.join('?' * len(RECEIPT_COLS))})",
                ("plan_ghost", 42, DIGEST_A, "apr_12", "log_ghost",
                 "{}", "2026-09-14T09:00:00+08:00"))
            # …then the probe body refuses the contradictory replay.
            PublisherService(db2, None)._probe_in_open_transaction(REQUEST)
    db2.close()
    assert db_digest(path) == before          # logical state unchanged
    fresh = Database(path)
    assert fresh.conn.execute(
        "SELECT COUNT(*) FROM publication_receipt").fetchone()[0] == 1
    fresh.close()


# ---------------------------------------------------------------- 1.4


def _barrier_connections(path, n=2):
    conns = [Database(path) for _ in range(n)]
    barrier = threading.Barrier(n)
    return conns, barrier


def _race_threads(fns, timeout=15.0):
    """Run each fn in its own thread, concurrently; join all; re-raise
    the first captured exception. box[i] holds (kind, value)."""
    boxes = [{} for _ in fns]

    def target(i, fn):
        try:
            boxes[i]["ok"] = fn()
        except BaseException as e:      # noqa: BLE001 — recorded, re-raised
            boxes[i]["err"] = e
    threads = [threading.Thread(target=target, args=(i, fn), daemon=True)
               for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.1, deadline - time.monotonic()))
        assert t.is_alive() is False, "race thread hung (lock starvation?)"
    for box in boxes:
        if "err" in box:
            raise box["err"]
    return [box["ok"] for box in boxes]


def test_case_a_two_connections_same_key_same_payload_both_replay(tmp_path):
    """§8 case 6 on the probe-only path: with the receipt already
    committed, both racers hit case A — identical responses, one row."""
    path = str(tmp_path / "t.db")
    db = Database(path)
    seed(db)
    db.close()
    conns, barrier = _barrier_connections(path)
    services = [PublisherService(c, None) for c in conns]

    def race(svc):
        barrier.wait(10)
        return svc.probe(REQUEST)

    results = _race_threads([lambda s=s: race(s) for s in services])
    assert [r.kind for r in results] == ["replay", "replay"]
    assert results[0].response == results[1].response == STORED_RESPONSE
    probe = sqlite3.connect(path)
    assert probe.execute("SELECT COUNT(*) FROM idempotency_registry"
                         ).fetchone()[0] == 1
    assert probe.execute("SELECT COUNT(*) FROM publication_receipt"
                         ).fetchone()[0] == 1
    probe.close()
    for c in conns:
        c.close()


def test_case_b_two_connections_same_key_different_payload(tmp_path):
    """§8 case 7 on the probe-only path: the seeded request replays;
    the challenger conflicts; the loser changed NOTHING — asserted as an
    identical normalised logical state (canonical dump of every table:
    registry, receipt, audit_chain, authority/lifecycle included).
    Deliberately not claimed as byte-identical files: SQLite page layout
    is not part of the logical state."""
    path = str(tmp_path / "t.db")
    db = Database(path)
    seed(db)
    db.close()
    before = db_digest(path)
    challenger = {**REQUEST, "plan_digest": DIGEST_C}
    conns, barrier = _barrier_connections(path)
    services = [PublisherService(c, None) for c in conns]

    def race(svc, request):
        barrier.wait(10)
        try:
            return ("ok", svc.probe(request))
        except PublishIdempotencyConflictError as e:
            return ("conflict", e)

    r1, r2 = _race_threads([
        lambda: race(services[0], REQUEST),
        lambda: race(services[1], challenger),
    ])
    kinds = sorted([r1[0] if r1[0] == "conflict" else r1[1].kind,
                    r2[0] if r2[0] == "conflict" else r2[1].kind])
    assert kinds == ["conflict", "replay"]
    conflict = (r1 if r1[0] == "conflict" else r2)[1]
    assert conflict.to_error_details()["original_plan_id"] == "plan_7f3a91"
    assert db_digest(path) == before          # logical state unchanged
    for c in conns:
        c.close()


def test_case_b_conflict_rolls_back_writes_in_same_txn(tmp_path):
    """Zero-change is structural: a conflict raised inside a BEGIN
    rolls it back, so even a mid-transaction write cannot leak. This
    mirrors what the Phase 2 full transaction does around the probe
    body: the write and the raising probe share ONE BEGIN IMMEDIATE."""
    path = str(tmp_path / "t.db")
    db = Database(path)
    seed(db)
    before = db_digest(path)
    challenger = {**REQUEST, "plan_digest": DIGEST_D}
    ps = PublisherService(db, None)
    fingerprint = request_fingerprint(challenger)
    with pytest.raises(PublishIdempotencyConflictError):
        with db.transaction():
            # A hypothetical Phase 2 write inside the same transaction as
            # the probe…
            db.conn.execute(
                "INSERT INTO publication_receipt "
                f"({', '.join(RECEIPT_COLS)}) "
                f"VALUES ({', '.join('?' * len(RECEIPT_COLS))})",
                ("plan_ghost", 99, REQUEST["plan_digest"],
                 "apr_12", "log_ghost", "{}", "2026-09-14T09:00:00+08:00"))
            # …then the probe body hits a different fingerprint and raises.
            result = ps._probe_in_open_transaction(challenger, fingerprint)
            raise AssertionError(f"unreachable: {result}")
    assert db_digest(path) == before          # ghost write rolled back
    # the standalone public probe agrees the same input conflicts.
    with pytest.raises(PublishIdempotencyConflictError):
        ps.probe(challenger)
    db.close()
