"""G2 tasks 1.2–1.4 — canonical fingerprint and the idempotency probe.

1.2: ``request_fingerprint`` = sha256 over ``canonical()`` of the exact
five contract fields (same canonical the audit chain uses). Key-order
independence and any-field sensitivity are pinned here, plus the
fail-closed guard against malformed requests.
1.3: ``PublisherService.probe`` = design §4 steps 3–4 only.
1.4: §5 cases A/B (§8 case 6/7 pre-wired on the probe-only path):
two barrier-synced connections racing the same key — same payload →
both replay identically, exactly one registry row; different payload →
one wins, the other conflicts, and NOTHING on the losing side changed
(registry, receipt, audit_chain, lifecycle — zero-touch asserted via
whole-DB byte hash).
"""
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from planpilot.persistence import Database, canonical
from planpilot.publisher import (
    REQUEST_FIELDS,
    PublishIdempotencyConflictError,
    PublisherService,
    request_fingerprint,
)
from planpilot.store.errors import IdempotencyConflictError, StoreInvariantError

KEY = "idem-4471e5c29f084cd3b7fe682a7a142df4"
REQUEST = {
    "plan_id": "plan_7f3a91",
    "expected_plan_version": 3,
    "plan_digest": "sha256:" + "a" * 64,
    "approval_set_id": "apr_12",
    "idempotency_key": KEY,
}
STORED_RESPONSE = {
    "status": "PUBLISHED",
    "plan_id": "plan_7f3a91",
    "published_version": 7,
    "audit_log_id": "log_1",
}

REGISTRY_COLS = ("tool_name", "idempotency_key", "request_fingerprint",
                 "receipt_id", "created_at")
RECEIPT_COLS = ("plan_id", "plan_version", "plan_digest", "approval_set_id",
                "audit_log_id", "response_json", "created_at")


def seed(db, response=STORED_RESPONSE, fingerprint=None, key=KEY,
         audit_log_id="log_1", plan_id="plan_7f3a91", plan_version=3):
    """Write one receipt + one registry row through the production
    constraint surface (plain INSERTs under transaction())."""
    with db.transaction():
        c = db.conn
        c.execute(
            f"INSERT INTO publication_receipt ({', '.join(RECEIPT_COLS)}) "
            f"VALUES ({', '.join('?' * len(RECEIPT_COLS))})",
            (plan_id, plan_version, REQUEST["plan_digest"],
             REQUEST["approval_set_id"], audit_log_id,
             json.dumps(response, sort_keys=True),
             "2026-09-14T09:00:00+08:00"))
        c.execute(
            f"INSERT INTO idempotency_registry ({', '.join(REGISTRY_COLS)}) "
            f"VALUES ({', '.join('?' * len(REGISTRY_COLS))})",
            ("publish_plan", key,
             fingerprint or request_fingerprint(REQUEST), 1,
             "2026-09-14T09:00:00+08:00"))


def db_digest(path):
    """Byte-exact identity of the whole database file (canonical dump)."""
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
        return value + "x"
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
    assert db_digest(path) == before          # zero business change
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
        ps.probe({**REQUEST, "plan_digest": "sha256:" + "b" * 64})
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
    the challenger conflicts; the loser changed NOTHING — asserted as a
    whole-file digest, covering registry, receipt, audit_chain AND the
    authority/lifecycle tables in one shot."""
    path = str(tmp_path / "t.db")
    db = Database(path)
    seed(db)
    db.close()
    before = db_digest(path)
    challenger = {**REQUEST, "plan_digest": "sha256:" + "c" * 64}
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
    assert db_digest(path) == before          # byte-identical after race
    for c in conns:
        c.close()


def test_case_b_conflict_rolls_back_writes_in_same_txn(tmp_path):
    """Zero-change is structural: a conflict raised inside transaction()
    rolls the BEGIN back, so even a mid-transaction write cannot leak.
    This mirrors what the Phase 2 full transaction does around the probe:
    the write and the raising probe share ONE Database.transaction()."""
    path = str(tmp_path / "t.db")
    db = Database(path)
    seed(db)
    before = db_digest(path)
    challenger = {**REQUEST, "plan_digest": "sha256:" + "d" * 64}
    ps_probe = PublisherService(db, None)
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
            # …then the registry probe hits a different fingerprint.
            row = db.conn.execute(
                "SELECT ir.request_fingerprint, pr.plan_id, pr.plan_version"
                "  FROM idempotency_registry ir"
                "  JOIN publication_receipt pr ON pr.id = ir.receipt_id"
                " WHERE ir.tool_name = 'publish_plan'"
                "   AND ir.idempotency_key = ?",
                (challenger["idempotency_key"],)).fetchone()
            assert row[0] != fingerprint
            raise PublishIdempotencyConflictError(
                challenger["idempotency_key"], row[1], "PUBLISHED",
                original_plan_version=row[2])
    assert db_digest(path) == before          # ghost write rolled back
    # the probe module agrees the same input conflicts (same decision)
    with pytest.raises(PublishIdempotencyConflictError):
        ps_probe.probe(challenger)
    db.close()
