"""p2-5/§6 — the /publish HTTP route is a thin adapter over the shared
middleware + PublisherPreparedCall (design §4, §6.1–§6.3).

Reviewer red lines pinned here, each against a REAL running server:
- _auth("approve_publish") stays OUTSIDE the middleware: a non-planner
  gets the bare transport 403, never a tool_error envelope, and never a
  decision trace (execute() was not reached);
- the route no longer pre-validates: invalid payloads return the
  contract INVALID_INPUT envelope produced inside middleware.execute,
  which only the middleware path can emit;
- audit actor = authenticated principal["sub"], not "system";
- §6.3 status mapping: success 200 / INVALID_INPUT 400 / POLICY_VIOLATION
  403 / conflict-and-approval codes 409 / INTERNAL_ERROR 503;
- observer trace: exactly one decision_trace row per middleware-driven
  invocation, success AND failure, and none for auth-rejected calls;
- the window-closed publish entry returns the existing contract errors
  (APPROVAL_EXPIRED on publish after the clock passes expires_at;
  APPROVAL_WINDOW_CLOSED on the request entry past the guard);
- outcome.wire_bytes pass through verbatim: replay returns the
  byte-identical stored receipt with the same audit_log_id.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from planpilot.persistence import Database
from planpilot.security import issue_token

from unit.test_runtime_authority import (
    clock,
    install,
    signed_content,
)

ROOT = Path(__file__).resolve().parents[2]
SECRET = "p2-5-http-publish-secret-at-least-32-chars"


def _api_module():
    spec = importlib.util.spec_from_file_location("p25_api", ROOT / "tools/api_server.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def _start(api, db, clock_obj):
    server = api.Server(("127.0.0.1", 0), db, SECRET, ROOT / "data", clock=clock_obj)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _call(server, path, body=None, user="planner-a", role="planner", raw=None):
    headers = {"Content-Type": "application/json"}
    if role is not None:
        headers["Authorization"] = "Bearer " + issue_token(user, role, SECRET)
    if raw is None:
        raw = None if body is None else json.dumps(body).encode()
    request = Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=raw,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def _publish_body(content, set_id, key, *, digest=None, version=1):
    return {
        "plan_id": content["plan_id"],
        "expected_plan_version": version,
        "plan_digest": digest or content["plan_digest"],
        "approval_set_id": set_id,
        "idempotency_key": key,
    }


def _approved_set(server, db, content):
    """Approve the publish binding through the existing HTTP approval flow."""
    status, requested = _call(server, "/approval/request", {
        "plan_id": content["plan_id"], "plan_version": 1,
        "plan_digest": content["plan_digest"], "action": "publish_plan",
    })
    assert status == 200, requested
    for row in requested["approvals"]:
        status, decided = _call(server, "/approval/decide", {
            "request_id": row["approval_request_id"], "decision": "APPROVED",
        })
        assert status == 200, decided
    return requested["approval_set_id"]


def _trace_count(db):
    return db.conn.execute("SELECT COUNT(*) c FROM decision_traces").fetchone()["c"]


def _published_events(db):
    return db.conn.execute(
        "SELECT COUNT(*) c FROM audit_chain WHERE record LIKE '%plan_published%'"
    ).fetchone()["c"]


def _audit_records(db):
    return [json.loads(row["record"]) for row in db.conn.execute(
        "SELECT record FROM audit_chain ORDER BY id"
    ).fetchall()]


# ------------------------------------------------------------------ §6.2
#
# The route's ONE schema entry point is middleware.execute: a bad payload
# must come back as the contract tool_error envelope (proving execute
# produced it — the removed route pre-validation returned a bare 400
# {"error": ...}), with HTTP 400 and zero state touched.

def test_invalid_payload_returns_contract_envelope_not_bare_400(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    content = signed_content(fixtures)
    install(server.authority, content)
    try:
        before_traces = _trace_count(db)
        body = _publish_body(content, "APS" + "-x" * 20, "p25-bad-digest-key-0001",
                             digest="not-a-64-hex-digest")
        status, payload = _call(server, "/publish", body)
        assert status == 400
        # tool_error envelope fields — only middleware.execute emits these.
        assert payload["error_code"] == "INVALID_INPUT"
        assert payload["retryable"] is False
        assert set(payload) == {"error_code", "message", "retryable",
                                "correlation_id", "details"}
        # Zero business change… but the invocation WAS observed (§6.1).
        assert _published_events(db) == 0
        assert _trace_count(db) == before_traces + 1
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()


# ------------------------------------------------------------------ §4 step 1
#
# Non-planner: auth refused in the ROUTE (bare 403, no envelope, no
# middleware, no trace, no registry row) — _auth("approve_publish") must
# stay outside execute().

def test_non_planner_gets_transport_403_outside_the_middleware(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    content = signed_content(fixtures)
    install(server.authority, content)
    set_id = _approved_set(server, db, content)
    try:
        before = _trace_count(db)
        body = _publish_body(content, set_id, "p25-nonplanner-key-00001")
        status, payload = _call(server, "/publish", body, role="manager")
        assert status == 403
        # Bare transport error, NOT a tool_error envelope:
        assert "error_code" not in payload and "correlation_id" not in payload
        # Auth was rejected before execute() — the observer never fired.
        assert _trace_count(db) == before
        with db.lock:
            registry = db.conn.execute(
                "SELECT COUNT(*) c FROM idempotency_registry").fetchone()["c"]
        assert registry == 0
        # And a planner still can publish afterwards (no state poisoned).
        status, ok = _call(server, "/publish", body, role="planner")
        assert status == 200 and ok["status"] == "PUBLISHED"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()


# ------------------------------------------------------------------ §6.3
#
# Every error_code the route can produce maps through the §6.3 table.

def test_status_mapping_across_the_error_families(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    content = signed_content(fixtures)
    install(server.authority, content)
    set_id = _approved_set(server, db, content)
    try:
        # 409 IDEMPOTENCY_CONFLICT (§5 case B): same key, different body.
        first = _publish_body(content, set_id, "p25-map-first-key-000001")
        assert _call(server, "/publish", first)[0] == 200
        clash = dict(first, plan_digest="a" * 64,
                     idempotency_key="p25-map-first-key-000001")
        status, payload = _call(server, "/publish", clash)
        assert status == 409 and payload["error_code"] == "IDEMPOTENCY_CONFLICT"

        # 409 PLAN_DIGEST_MISMATCH: fresh key, lying digest.
        status, payload = _call(server, "/publish", _publish_body(
            content, set_id, "p25-map-digest-key-0000001", digest="b" * 64))
        assert status == 409 and payload["error_code"] == "PLAN_DIGEST_MISMATCH"

        # 409 STATE_NOT_FOUND: unknown plan (old StoreError row of §6.3).
        status, payload = _call(server, "/publish", {
            "plan_id": "PLAN-NOPE", "expected_plan_version": 1,
            "plan_digest": "c" * 64, "approval_set_id": set_id,
            "idempotency_key": "p25-map-notfound-key-0001",
        })
        assert status == 409 and payload["error_code"] == "STATE_NOT_FOUND"

        # 403 POLICY_VIOLATION (Case D): tamper the durable receipt's
        # approval set — a second live set for one binding is impossible
        # through the service (binding-idempotent), so the DB layer is
        # the honest injection point (matrix precedent).
        with db.transaction():
            db.conn.execute(
                "UPDATE publication_receipt SET approval_set_id=? "
                "WHERE plan_id=? AND plan_version=1",
                ("APS-TAMPERED-SET-ID-000000000000000000000000", content["plan_id"]),
            )
        status, payload = _call(server, "/publish", _publish_body(
            content, set_id, "p25-map-policy-key-0000001"))
        assert status == 403 and payload["error_code"] == "POLICY_VIOLATION"
        assert payload["details"]["violated_policy"] == "approval_scope_exceeded"

        # 503 INTERNAL_ERROR: poisoned authority fails closed at the
        # prepare entry — the code rides the fall-through, not a 500.
        server.authority._poisoned = True
        try:
            status, payload = _call(server, "/publish", _publish_body(
                content, set_id, "p25-map-poison-key-00001"))
            assert status == 503 and payload["error_code"] == "INTERNAL_ERROR"
        finally:
            server.authority._poisoned = False
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()


def test_mapping_table_covers_the_registered_codes(tmp_path):
    """§6.3 completeness pin: every registered code either maps explicitly
    or provably cannot occur on the publish path; nothing maps outside the
    table's four statuses, and the route's unmapped fallback is 503."""
    api = _api_module()
    from planpilot.tools.registry import error_specs
    table = api.PUBLISH_STATUS_BY_ERROR_CODE
    assert set(table.values()) <= {400, 403, 409, 503}
    unreachable = {"NO_FEASIBLE_PLAN", "RATE_LIMITED", "SEARCH_ESCALATION_EXHAUSTED"}
    for code in error_specs():
        if code in unreachable:
            assert code not in table, f"{code} must stay unmapped on publish"
        else:
            assert code in table, f"{code} missing from the §6.3 mapping"
    # The reviewer-pinned rows:
    assert table["INVALID_INPUT"] == 400 and table["VALIDATION_FAILED"] == 400
    assert table["POLICY_VIOLATION"] == 403
    assert table["APPROVAL_SET_INVALIDATED"] == 409  # NOT 403/POLICY_VIOLATION
    assert table["APPROVAL_WINDOW_CLOSED"] == 409
    assert table["INTERNAL_ERROR"] == 503 and table["DEADLINE_EXCEEDED"] == 503


# ------------------------------------------------------------------ p2-5 actor
#
# plan_published must carry the AUTHENTICATED principal, not "system".
# Precise wording (reviewer correction): the AUDIT RECORD is what stores
# the actor. The publication receipt has no actor field — it binds the
# actor via its audit_log_id pointer to exactly this record — and the
# decision-trace schema has no actor field either (an invocation's
# identity is proven by its correlation_id; the trace shows the call
# was observed, not who made it).

def test_audit_actor_is_the_authenticated_principal(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    content = signed_content(fixtures)
    install(server.authority, content)
    set_id = _approved_set(server, db, content)
    try:
        status, body = _call(
            server, "/publish",
            _publish_body(content, set_id, "p25-actor-key-000000001"),
            user="grace-hopper",
        )
        assert status == 200 and body["status"] == "PUBLISHED"
        published = db.conn.execute(
            "SELECT id, record FROM audit_chain "
            "WHERE json_extract(record, '$.event') = 'plan_published'"
        ).fetchall()
        assert len(published) == 1
        record = json.loads(published[0]["record"])
        assert record["actor"] == "grace-hopper"
        # The receipt carries no actor of its own; it BINDS the actor by
        # pointing at exactly this audit record. audit_log_id is the
        # chain rowid in AUD-<n:012d> form (persistence._append_audit).
        audit_id = f"AUD-{published[0]['id']:012d}"
        assert record["plan_id"] == body["plan_id"]
        assert body["audit_log_id"] == audit_id
        receipt = db.conn.execute(
            "SELECT * FROM publication_receipt WHERE plan_id = ?",
            (body["plan_id"],)).fetchone()
        assert receipt is not None
        assert receipt["audit_log_id"] == audit_id
        # And the response is the contract receipt shape produced by the
        # publisher/middleware (never the old lifecycle dict).
        assert set(body) == {"plan_id", "published_version", "status",
                             "audit_log_id"}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()


# ------------------------------------------------------------------ §6.1 observer
#
# One decision_trace per execute — success, replay and failure alike —
# and zero change on the failed calls.

def test_observer_trace_per_invocation(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    content = signed_content(fixtures)
    install(server.authority, content)
    set_id = _approved_set(server, db, content)
    try:
        body = _publish_body(content, set_id, "p25-trace-key-000000001")
        base = _trace_count(db)
        assert _call(server, "/publish", body)[0] == 200          # first
        assert _trace_count(db) == base + 1
        status, replay = _call(server, "/publish", body)           # case A replay
        assert status == 200
        assert _trace_count(db) == base + 2
        assert _call(server, "/publish",
                     dict(body, plan_digest="d" * 64))[0] == 409   # case B
        assert _trace_count(db) == base + 3
        # Replays/conflicts added traces but never a second plan_published
        # event, and every trace row is readable back from the table.
        assert _published_events(db) == 1
        rows = db.conn.execute(
            "SELECT record FROM decision_traces").fetchall()
        assert len(rows) == _trace_count(db)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()


# ------------------------------------------------------------------ window closed
#
# §8 case 12's window-closed legs at the publish ENTRY. Pinned semantics
# (approval-service _refresh_expiry only touches PENDING requests — an
# already-APPROVED set never turns EXPIRED, test_server_clock_policy
# pins the same rule at the authority layer): a PENDING approval that
# crosses expires_at blocks publication APPROVAL_EXPIRED, and a request
# past horizon_guard + 24h is refused APPROVAL_WINDOW_CLOSED at the
# creating entry. Both ride the 409 row with zero business change.

def test_closed_window_blocks_publish_entry(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    content = signed_content(fixtures)
    install(server.authority, content)
    status, requested = _call(server, "/approval/request", {
        "plan_id": content["plan_id"], "plan_version": 1,
        "plan_digest": content["plan_digest"], "action": "publish_plan",
    })
    assert status == 200
    expires_at = requested["approvals"][0]["expires_at"]
    try:
        # Nothing decided; the clock crosses the expiry boundary: the
        # publish entry refuses the stale set (case 12 APPROVAL_EXPIRED).
        fixed._now = expires_at
        before = _published_events(db)
        status, payload = _call(server, "/publish", _publish_body(
            content, requested["approval_set_id"], "p25-window-key-00000001"))
        assert status == 409
        assert payload["error_code"] == "APPROVAL_EXPIRED"
        assert _published_events(db) == before

        # Deeper into the guard: a request for a NEW binding (the
        # idempotent early-return reuses existing sets before the guard)
        # is refused APPROVAL_WINDOW_CLOSED at the creating entry
        # (request-construction leg of case 12).
        second = signed_content(fixtures, plan_id="PLAN-RUNTIME-2")
        install(server.authority, second)
        fixed._now = "2026-09-19T18:39:00+08:00"
        status, payload = _call(server, "/approval/request", {
            "plan_id": second["plan_id"], "plan_version": 1,
            "plan_digest": second["plan_digest"], "action": "publish_plan",
        })
        assert status == 409
        assert payload["error_code"] == "APPROVAL_WINDOW_CLOSED"
        assert payload["details"]["horizon_guard"] == "2026-09-19T17:00:00+08:00"
        # A PENDING set at this stamp is likewise dead to publication:
        # same 409 family, code stays the honest one (not POLICY_VIOLATION).
        fixed._now = expires_at
        status, payload = _call(server, "/publish", _publish_body(
            content, requested["approval_set_id"], "p25-window-key-2-000001"))
        assert status == 409 and payload["error_code"] == "APPROVAL_EXPIRED"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()


# ------------------------------------------------------------------ §6.3 wire verbatim
#
# Replay returns the stored receipt BYTE-IDENTICAL — proof the route
# writes outcome.wire_bytes instead of re-serialising.

def test_replay_bytes_are_verbatim(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    content = signed_content(fixtures)
    install(server.authority, content)
    set_id = _approved_set(server, db, content)

    def raw_call(path, body, user="planner-a", role="planner"):
        request = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + issue_token(user, role, SECRET)},
        )
        with urlopen(request, timeout=15) as response:
            return response.read()
    try:
        body = _publish_body(content, set_id, "p25-bytes-key-000000001")
        first_bytes = raw_call("/publish", body)
        second_bytes = raw_call("/publish", body)
        assert first_bytes == second_bytes
        receipt = json.loads(first_bytes)
        with db.lock:
            stored = db.conn.execute(
                "SELECT response_json FROM publication_receipt "
                "WHERE plan_id=? AND plan_version=1",
                (content["plan_id"],),
            ).fetchone()["response_json"]
        assert stored == json.dumps(receipt, sort_keys=True,
                                    separators=(",", ":"))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()


# ------------------------------------------------- reviewer P1: non-object JSON
#
# The global object guard used to reject arrays/strings/numbers/null
# with a bare {"error": ...} 400 BEFORE the route reached the
# middleware — bypassing the contract envelope and the observer. Fixed
# dispatch order: /publish forwards ANY parsed JSON value to
# middleware.execute (reviewer red line: it is the single validation
# entry), so these calls now answer 400 + INVALID_INPUT envelope +
# exactly one trace, with zero state touched.

def test_non_object_json_bodies_get_the_contract_envelope(tmp_path, fixtures):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server, thread = _start(api, db, fixed)
    try:
        for raw in (b'["not","an","object"]', b'"just-a-string"',
                    b'12345', b'null'):
            before_traces = _trace_count(db)
            status, payload = _call(server, "/publish", raw=raw)
            assert status == 400, raw
            assert payload["error_code"] == "INVALID_INPUT", raw
            assert payload["retryable"] is False, raw
            assert set(payload) == {"error_code", "message", "retryable",
                                    "correlation_id", "details"}, raw
            # The invocation WAS observed (§6.1) and nothing was written.
            assert _trace_count(db) == before_traces + 1, raw
            assert _published_events(db) == 0, raw
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); db.close()
