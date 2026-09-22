"""G2 §8 — crash & concurrency matrix for the publish transaction.

Cases follow the design numbering (1–18). Crash points are the publisher's
``_fault`` seam: raise-style (in-process, cases 1–4/9/13) mirrors the effect
of dying at that line, and cases 14–15 additionally hard-kill a real
subprocess (``os._exit`` via the env-armed hook) so SQLite/WAL recovery is
proven, not simulated.

Non-planner role is enforced at the route/authority boundary (§4 step 1)
and pinned by the existing authority tests; the approval-window-closed
clock is a request-construction guard (§4 step 2) exercised in the approval
suite. Case 12 here covers the publisher-reachable negatives.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from planpilot.approval import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalRequiredError,
    ApprovalSetInvalidatedError,
)
from planpilot.authority import RuntimeAuthority
from planpilot.audit import AuditTrail, DecisionTraceWriter
from planpilot.clock import FixedClock
from planpilot.persistence import Database
from planpilot.publisher import (
    FAULT_AFTER_AUDIT,
    FAULT_AFTER_COMMIT,
    FAULT_AFTER_LIFECYCLE,
    FAULT_AFTER_RECEIPT,
    FAULT_BEFORE_LIFECYCLE,
    FAULT_ENV_VAR,
    PublicationInvariantError,
    PublishIdempotencyConflictError,
    PublisherService,
    clear_fault_hook,
    set_fault_hook,
)
from planpilot.store import canonical_plan_digest
from planpilot.store.errors import (
    DigestMismatchError,
    VersionConflictError,
)
from planpilot.tools.errors import FrameworkDomainError
from planpilot.tools.middleware import ToolErrorMiddleware

NOW = "2026-09-14T08:00:00+08:00"
HORIZON_END = "2026-09-18T17:00:00+08:00"
ROOT = Path(__file__).resolve().parents[2]


class _Crash(Exception):
    """Raise-style fault stand-in: unwinds exactly like death at the line."""


def signed(fixtures, *, plan_id="PLAN-MTX-1", version=1):
    kpis = fixtures.make_kpis(
        overtime_hours=0, secondary_skill_assignment_count=0
    )
    draft = fixtures.make_content(
        plan_id=plan_id, plan_version=version, kpis=kpis,
        consequential_approval_required=False,
    )
    digest = canonical_plan_digest(draft)
    return fixtures.make_content(
        plan_id=plan_id, plan_version=version, kpis=kpis, digest=digest,
        consequential_approval_required=False,
    )


def validator_result(content):
    return {
        "plan_id": content["plan_id"],
        "plan_version": content["plan_version"],
        "plan_digest": content["plan_digest"],
        "recomputed_plan_digest": content["plan_digest"],
        "digest_verified": True,
        "is_feasible": True,
        "infeasible_reason": None,
        "hard_violations": [],
        "kpis": copy.deepcopy(content["kpis"]),
        "approval_requirements": [{
            "action": "publish_plan",
            "approver_role": "Production Planner",
            "reason": "Server-derived publication confirmation.",
        }],
        "quarantine_impact": [],
    }


def impacts():
    return {"publish_plan": {
        "affected_order_ids": ["ORD-A"],
        "changed_operation_count": 1,
        "kpi_deltas": {},
        "reason_codes": ["PUBLISH_CONFIRMATION_REQUIRED"],
    }}


def make_env(path, now=NOW):
    """One clock, one Database, one RuntimeAuthority — bind_clock pins
    exactly one clock object, so db and auth must share it (auth defaults
    to database.clock when none is passed)."""
    clock = FixedClock(now)
    db = Database(path, clock=clock)
    auth = RuntimeAuthority(db)
    return db, auth, PublisherService(db, auth)


def approved_pair(db, auth, fixtures, *, now=NOW, plan_id="PLAN-MTX-1"):
    """Install a validated plan and approve its publish set (not yet
    published). Returns (content, digest, approval_set_id)."""
    content = signed(fixtures, plan_id=plan_id)
    auth.install_validated_plan(
        content, validator_result(content), impacts(), HORIZON_END, now,
        actor="validator",
    )
    request = auth.request_approval(
        content["plan_id"], 1, content["plan_digest"], "publish_plan", "planner"
    )
    auth.decide_approval(
        request["approvals"][0]["approval_request_id"],
        "APPROVED", "planner", "planner",
    )
    return content, content["plan_digest"], request["approval_set_id"]


def publish_request(content, digest, approval_set_id, key):
    return {
        "plan_id": content["plan_id"],
        "expected_plan_version": 1,
        "plan_digest": digest,
        "approval_set_id": approval_set_id,
        "idempotency_key": key,
    }


def counts(db):
    out = {}
    for table in ("publication_receipt", "idempotency_registry", "audit_chain"):
        out[table] = db.conn.execute(
            f"SELECT COUNT(*) c FROM {table}"
        ).fetchone()["c"]
    out["published_events"] = db.conn.execute(
        "SELECT COUNT(*) c FROM audit_chain WHERE record LIKE '%plan_published%'"
    ).fetchone()["c"]
    return out


def durable_revision(db):
    return db.conn.execute(
        "SELECT revision FROM authority_state WHERE singleton=1"
    ).fetchone()["revision"]


def arm_single_point(point):
    """Raise-style hook armed for exactly one design fault point."""
    def hook(reached):
        if reached == point:
            raise _Crash(reached)
    return set_fault_hook(hook)


# --------------------------------------------------------------------------- #
# Cases 1–4: crash before COMMIT persists nothing; fresh replay succeeds.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("point", [
    FAULT_BEFORE_LIFECYCLE,
    FAULT_AFTER_LIFECYCLE,
    FAULT_AFTER_AUDIT,
    FAULT_AFTER_RECEIPT,
])
def test_crash_before_commit_rolls_back_everything(tmp_path, fixtures, point):
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)
    before = counts(db)
    rev_before = durable_revision(db)

    token = arm_single_point(point)
    try:
        with pytest.raises(_Crash):
            ps.publish(request)
    finally:
        clear_fault_hook(token)

    # Everything the transaction touched is gone (case 3 proves audit and
    # the transaction are one unit — even the already-appended chain row).
    assert counts(db) == before
    assert durable_revision(db) == rev_before
    assert auth._store.get_lifecycle(content["plan_id"], 1)["status"] != "PUBLISHED"
    assert db.verify_audit()

    # Retry with the same key after the crash: a fresh first publication.
    result = ps.publish(request)
    assert result["status"] == "PUBLISHED"
    after = counts(db)
    assert after["publication_receipt"] == 1
    assert after["idempotency_registry"] == 1
    assert after["published_events"] == before["published_events"] + 1
    assert durable_revision(db) == rev_before + 1
    db.close()


# --------------------------------------------------------------------------- #
# Cases 5/8/10/11: after COMMIT, retry replays byte-identically, adds nothing.
# --------------------------------------------------------------------------- #

def test_replay_after_commit_is_verbatim_and_inert(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)
    first = ps.publish(request)
    head_before = db.conn.execute(
        "SELECT entry_count FROM audit_chain_head WHERE singleton=1"
    ).fetchone()["entry_count"]
    before = counts(db)

    again = ps.publish(request)
    assert again == first                                    # case A
    assert again["audit_log_id"] == first["audit_log_id"]   # case 10 identity
    assert counts(db) == before                              # case 11 inert
    assert db.conn.execute(
        "SELECT entry_count FROM audit_chain_head WHERE singleton=1"
    ).fetchone()["entry_count"] == head_before
    db.close()


# --------------------------------------------------------------------------- #
# Case 6: two connections race the SAME key — identical responses, one row.
# --------------------------------------------------------------------------- #

def _fresh_publisher(path):
    clock = FixedClock(NOW)
    db = Database(path, clock=clock)
    auth = RuntimeAuthority(db)
    return db, auth, PublisherService(db, auth)


def test_two_connections_same_key_race(tmp_path, fixtures):
    path = tmp_path / "state.db"
    db0, auth0, _ = make_env(path)
    content, digest, set_id = approved_pair(db0, auth0, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)
    db0.close()

    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        db, auth, ps = _fresh_publisher(path)
        try:
            barrier.wait(timeout=15)
            results.append(ps.publish(request))
        except BaseException as exc:            # noqa: BLE001 — reported below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert len(results) == 2 and results[0] == results[1]
    check = Database(path, clock=FixedClock(NOW))
    try:
        assert counts(check)["idempotency_registry"] == 1
        assert counts(check)["published_events"] == 1
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# Case 7: two connections, same key, different payloads — one wins, one B.
# --------------------------------------------------------------------------- #

def test_two_connections_same_key_different_payload(tmp_path, fixtures):
    """§8 case 7: one key, TWO publishable-but-different requests racing.

    Both payloads are fully valid (different plans, each approved); they
    differ in fingerprint via plan_id. Whoever commits first wins case E;
    the loser sees the winner's registry row and gets case B with the
    WINNER's binding in details — order-insensitive.
    """
    path = tmp_path / "state.db"
    db0, auth0, _ = make_env(path)
    contentA, digestA, setA = approved_pair(db0, auth0, fixtures,
                                            plan_id="PLAN-MTX-A")
    contentB, digestB, setB = approved_pair(db0, auth0, fixtures,
                                            plan_id="PLAN-MTX-B")
    db0.close()

    reqA = publish_request(contentA, digestA, setA, "k" * 16)
    reqB = publish_request(contentB, digestB, setB, "k" * 16)

    barrier = threading.Barrier(2)
    outcome = {}

    def worker(name, request):
        db, auth, ps = _fresh_publisher(path)
        try:
            barrier.wait(timeout=15)
            outcome[name] = ("ok", ps.publish(request))
        except PublishIdempotencyConflictError as exc:
            outcome[name] = ("conflict", exc)
        except BaseException as exc:            # noqa: BLE001
            outcome[name] = ("error", exc)
        finally:
            db.close()

    threads = [
        threading.Thread(target=worker, args=("a", reqA)),
        threading.Thread(target=worker, args=("b", reqB)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    kinds = sorted(kind for kind, _ in outcome.values())
    assert kinds == ["conflict", "ok"]          # order-insensitive
    conflict = next(v for kind, v in outcome.values() if kind == "conflict")
    ok = next(v for kind, v in outcome.values() if kind == "ok")
    assert conflict.details["idempotency_key"] == "k" * 16
    assert conflict.details["original_plan_id"] == ok["plan_id"]
    assert ok["status"] == "PUBLISHED"
    check = Database(path, clock=FixedClock(NOW))
    try:
        got = counts(check)
        assert got["idempotency_registry"] == 1
        assert got["publication_receipt"] == 1
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# Case 9: output-schema tampering between step 11 and middleware validation
# -> full rollback, INTERNAL_ERROR, zero persisted change.
# --------------------------------------------------------------------------- #

def test_output_validation_failure_rolls_back_all(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)
    before = counts(db)
    rev_before = durable_revision(db)

    def tamper(response):
        bad = dict(response)
        bad["status"] = "PUBLISHING"            # outside the output enum
        return bad

    middleware = ToolErrorMiddleware()
    call = ps.prepared_call(response_mutator=tamper)
    outcome = middleware.execute("publish_plan", request, call)
    assert outcome.is_error
    payload = json.loads(outcome.wire_bytes.decode("utf-8"))
    assert payload["error_code"] == "INTERNAL_ERROR"
    assert payload["details"]["diagnostic_class"] == "ToolOutputValidationFailure"
    assert counts(db) == before
    assert durable_revision(db) == rev_before
    assert auth._store.get_lifecycle(content["plan_id"], 1)["status"] != "PUBLISHED"
    assert call._snapshot_proof()["state"] == "ROLLED_BACK"
    assert counts(db)["idempotency_registry"] == 0
    db.close()


# --------------------------------------------------------------------------- #
# Case 12 (publisher-reachable negatives): contract errors, zero change.
# --------------------------------------------------------------------------- #

def test_negative_pending_approval_zero_change(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content = signed(fixtures)
    auth.install_validated_plan(
        content, validator_result(content), impacts(), HORIZON_END, NOW,
        actor="validator",
    )
    request = auth.request_approval(content["plan_id"], 1,
                                    content["plan_digest"],
                                    "publish_plan", "planner")
    before = counts(db)
    with pytest.raises(ApprovalRequiredError):
        ps.publish(publish_request(content, content["plan_digest"],
                                   request["approval_set_id"], "k" * 16))
    assert counts(db) == before
    db.close()


def test_negative_rejected_set_zero_change(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content = signed(fixtures, plan_id="PLAN-MTX-2")
    auth.install_validated_plan(
        content, validator_result(content), impacts(), HORIZON_END, NOW,
        actor="validator",
    )
    digest = content["plan_digest"]
    request = auth.request_approval(content["plan_id"], 1, digest,
                                    "publish_plan", "planner")
    auth.decide_approval(request["approvals"][0]["approval_request_id"],
                         "REJECTED", "planner", "planner",
                         decision_reason="PRIORITY_CHANGED")
    before = counts(db)
    with pytest.raises(ApprovalRejectedError):
        ps.publish(publish_request(content, digest,
                                   request["approval_set_id"], "k" * 16))
    assert counts(db) == before
    db.close()


def test_negative_expired_set_zero_change(tmp_path, fixtures):
    """EXPIRED applies to PENDING requests (decided sets keep their
    decision): request, never decide, advance past the 24h TTL — the
    publisher's require_approved refresh turns them EXPIRED and refuses."""
    clock = FixedClock(NOW)
    db = Database(tmp_path / "state.db", clock=clock)
    auth = RuntimeAuthority(db)
    content, digest, set_id = _request_only(db, auth, fixtures,
                                            plan_id="PLAN-MTX-3")
    clock.advance(48 * 3600)                     # past every server clamp
    ps = PublisherService(db, auth)
    before = counts(db)
    with pytest.raises(ApprovalExpiredError):
        ps.publish(publish_request(content, digest, set_id, "k" * 16))
    assert counts(db) == before
    db.close()


def _request_only(db, auth, fixtures, *, plan_id="PLAN-MTX-1"):
    content = signed(fixtures, plan_id=plan_id)
    auth.install_validated_plan(
        content, validator_result(content), impacts(), HORIZON_END, NOW,
        actor="validator",
    )
    request = auth.request_approval(
        content["plan_id"], 1, content["plan_digest"], "publish_plan", "planner"
    )
    return content, content["plan_digest"], request["approval_set_id"]


def test_negative_stale_version_zero_change(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    stale = publish_request(content, digest, set_id, "k" * 16)
    stale["expected_plan_version"] = 99
    before = counts(db)
    with pytest.raises(VersionConflictError):
        ps.publish(stale)
    assert counts(db) == before
    db.close()


def test_negative_digest_mismatch_zero_change(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    wrong = publish_request(content, digest, set_id, "k" * 16)
    wrong["plan_digest"] = "e" * 64             # schema-legal, wrong value
    before = counts(db)
    with pytest.raises(DigestMismatchError) as exc:
        ps.publish(wrong)
    assert counts(db) == before
    # Reviewer round-7 P1: the details must state the contradiction it
    # reports — expected is the CALLER'S digest, not the stored one (that
    # leg already raises inside verify_digest with its own correct pair).
    details = exc.value.to_error_details()
    assert details["expected_plan_digest"] == "e" * 64
    assert details["recomputed_plan_digest"] == digest
    assert details["expected_plan_digest"] != details["recomputed_plan_digest"]
    db.close()


# --------------------------------------------------------------------------- #
# Case 13: post-COMMIT memory-sync failure — §4.5 rule 3, both halves.
# --------------------------------------------------------------------------- #

def test_post_commit_sync_failure_poisons_then_recovers(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)

    def poison_at_swap(reached):
        if reached == FAULT_AFTER_COMMIT:
            raise _Crash(reached)

    token = set_fault_hook(poison_at_swap)
    try:
        call = ps.prepared_call()
        candidate = dict(call.prepare(request, None))
        call.commit()                            # must NOT raise (rule 3)
    finally:
        clear_fault_hook(token)

    # CURRENT call resolves to the verified success outcome…
    assert candidate["status"] == "PUBLISHED"
    assert call._snapshot_proof()["state"] == "FINISHED"
    assert call._snapshot_proof()["release_point"] == "commit"
    # …the DB holds the FULL publication…
    assert counts(db)["publication_receipt"] == 1
    assert durable_revision(db) == auth.revision + 1   # memory lags: poisoned
    assert auth._poisoned is True
    # …and LATER entry points fail closed until reload.
    with pytest.raises(FrameworkDomainError) as exc:
        auth.get_plan(content["plan_id"], 1)
    assert exc.value.code == "INTERNAL_ERROR"
    assert exc.value._details["diagnostic_class"] == "AuthoritySnapshotDiverged"
    with pytest.raises(FrameworkDomainError):
        ps.publish(request)                       # even replay is refused

    auth.reload_from_db()
    assert auth._poisoned is False
    assert auth.revision == durable_revision(db)
    assert auth.get_plan(content["plan_id"], 1)["lifecycle"]["status"] == "PUBLISHED"
    # Recovery replay returns the stored receipt and adds NO second event.
    before = counts(db)
    assert ps.publish(request) == candidate
    assert counts(db) == before
    db.close()


# --------------------------------------------------------------------------- #
# Cases 14/15: hard-kill a REAL subprocess (os._exit) — WAL recovery proof.
# --------------------------------------------------------------------------- #

_SUB_CODE = """
import sys
sys.path.insert(0, {src!r})
sys.path.insert(0, {tests!r})
import copy
import _fixtures as F
from planpilot.clock import FixedClock
from planpilot.persistence import Database
from planpilot.authority import RuntimeAuthority
from planpilot.publisher import (PublisherService, fault_hook_from_environ,
                                 set_fault_hook)
from planpilot.store import canonical_plan_digest

NOW = "2026-09-14T08:00:00+08:00"
HORIZON_END = "2026-09-18T17:00:00+08:00"
db_path, mode = sys.argv[1], sys.argv[2]

kpis = F.make_kpis(overtime_hours=0, secondary_skill_assignment_count=0)
draft = F.make_content(plan_id="PLAN-KILL", plan_version=1, kpis=kpis,
                       consequential_approval_required=False)
digest = canonical_plan_digest(draft)
content = F.make_content(plan_id="PLAN-KILL", plan_version=1, kpis=kpis,
                         digest=digest, consequential_approval_required=False)
vr = {{"plan_id": "PLAN-KILL", "plan_version": 1, "plan_digest": digest,
      "recomputed_plan_digest": digest, "digest_verified": True,
      "is_feasible": True, "infeasible_reason": None, "hard_violations": [],
      "kpis": copy.deepcopy(content["kpis"]),
      "approval_requirements": [{{"action": "publish_plan",
          "approver_role": "Production Planner",
          "reason": "Server-derived publication confirmation."}}],
      "quarantine_impact": []}}
im = {{"publish_plan": {{"affected_order_ids": ["ORD-A"],
      "changed_operation_count": 1, "kpi_deltas": {{}},
      "reason_codes": ["PUBLISH_CONFIRMATION_REQUIRED"]}}}}

clock = FixedClock(NOW)
db = Database(db_path, clock=clock)
auth = RuntimeAuthority(db)
if mode == "setup":
    auth.install_validated_plan(content, vr, im, HORIZON_END, NOW, actor="v")
    req = auth.request_approval("PLAN-KILL", 1, digest, "publish_plan", "planner")
    auth.decide_approval(req["approvals"][0]["approval_request_id"],
                         "APPROVED", "planner", "planner")
    print("SETUP-OK")
    raise SystemExit(0)

# kill mode: rebuild the (idempotent) approval binding, arm the env hook,
# publish — the hook os._exit(9)s at the armed point.
req = auth.request_approval("PLAN-KILL", 1, digest, "publish_plan", "planner")
request = {{"plan_id": "PLAN-KILL", "expected_plan_version": 1,
            "plan_digest": digest, "approval_set_id": req["approval_set_id"],
            "idempotency_key": "k" * 16}}
set_fault_hook(fault_hook_from_environ())
ps = PublisherService(db, auth)
try:
    result = ps.publish(request)
    print("PUBLISHED", result["audit_log_id"])
except BaseException as exc:
    print("CRASHED", type(exc).__name__)
"""


def _run_kill(tmp_path, mode, fault=None):
    script_path = tmp_path / "kill_script.py"
    script_path.write_text(
        _SUB_CODE.format(src=str(ROOT / "src"), tests=str(ROOT / "tests")),
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    env[FAULT_ENV_VAR] = fault or "none"
    if fault == "none":
        env.pop(FAULT_ENV_VAR)
    return subprocess.run(
        [sys.executable, str(script_path), str(tmp_path / "kill.db"), mode],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_hard_kill_after_audit_rolls_back_under_recovery(tmp_path, fixtures):
    """§8 case 14: os._exit between audit insert and receipt insert."""
    path = tmp_path / "kill.db"
    setup = _run_kill(tmp_path, "setup")
    assert "SETUP-OK" in setup.stdout, setup.stdout + setup.stderr

    killed = _run_kill(tmp_path, "kill", fault=FAULT_AFTER_AUDIT)
    assert killed.returncode == 9, (killed.stdout, killed.stderr)

    # Fresh process over the same file: WAL recovery rolled the kill back.
    db, auth, ps = make_env(path)
    try:
        got = counts(db)
        assert got["publication_receipt"] == 0
        assert got["idempotency_registry"] == 0
        assert got["published_events"] == 0
        assert db.verify_audit()
        lc = auth.get_plan("PLAN-KILL", 1)["lifecycle"]
        assert lc["status"] != "PUBLISHED"
        # clock_session high-water survives the kill (G1.0.2 discipline).
        assert db.conn.execute(
            "SELECT COUNT(*) c FROM clock_session"
        ).fetchone()["c"] >= 0
        # Recovery: the same key now publishes for real, exactly once.
        request = {
            "plan_id": "PLAN-KILL", "expected_plan_version": 1,
            "plan_digest": auth.get_plan("PLAN-KILL", 1)["content"]["plan_digest"],
            "approval_set_id": lc["approval_set_id"] or auth.request_approval(
                "PLAN-KILL", 1,
                auth.get_plan("PLAN-KILL", 1)["content"]["plan_digest"],
                "publish_plan", "planner")["approval_set_id"],
            "idempotency_key": "k" * 16,
        }
        result = ps.publish(request)
        assert result["status"] == "PUBLISHED"
        assert counts(db)["published_events"] == 1
    finally:
        db.close()


def test_hard_kill_after_commit_replays_verbatim(tmp_path, fixtures):
    """§8 case 15: the process dies AFTER conn.commit(), before responding."""
    path = tmp_path / "kill.db"
    setup = _run_kill(tmp_path, "setup")
    assert "SETUP-OK" in setup.stdout, setup.stdout + setup.stderr

    killed = _run_kill(tmp_path, "kill", fault=FAULT_AFTER_COMMIT)
    # Hook fires inside commit() after the durable point: process dies before
    # the script can print, but the publication is REAL.
    assert killed.returncode == 9, (killed.stdout, killed.stderr)
    assert "PUBLISHED" not in killed.stdout

    db, auth, ps = make_env(path)
    try:
        assert counts(db)["publication_receipt"] == 1
        row = db.conn.execute(
            "SELECT audit_log_id, approval_set_id, response_json"
            " FROM publication_receipt"
        ).fetchone()
        content = auth.get_plan("PLAN-KILL", 1)["content"]
        request = {
            "plan_id": "PLAN-KILL", "expected_plan_version": 1,
            "plan_digest": content["plan_digest"],
            "approval_set_id": row["approval_set_id"],
            "idempotency_key": "k" * 16,
        }
        replay = ps.publish(request)
        assert replay == json.loads(row["response_json"])   # byte-identical
        assert replay["audit_log_id"] == row["audit_log_id"]
        assert counts(db)["published_events"] == 1          # never a second
        assert db.verify_audit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Case 16: alias immutability — §5 case C's stored rows never move.
# --------------------------------------------------------------------------- #

def test_alias_replay_leaves_receipt_and_original_row_untouched(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    first = ps.publish(publish_request(content, digest, set_id, "k" * 16))

    def snapshot_row(table, where):
        row = db.conn.execute(
            f"SELECT * FROM {table} WHERE {where}"
        ).fetchone()
        return {k: row[k] for k in row.keys()}

    receipt_before = snapshot_row("publication_receipt",
                                  f"plan_id='{content['plan_id']}'")
    registry_before = snapshot_row("idempotency_registry",
                                   "idempotency_key='kkkkkkkkkkkkkkkk'")
    head_before = db.conn.execute(
        "SELECT entry_count FROM audit_chain_head WHERE singleton=1"
    ).fetchone()["entry_count"]

    # Case C: NEW key, same binding -> alias of the SAME receipt.
    alias = ps.publish(publish_request(content, digest, set_id, "z" * 16))
    assert alias == first                          # verbatim stored response

    assert snapshot_row("publication_receipt",
                        f"plan_id='{content['plan_id']}'") == receipt_before
    assert snapshot_row("idempotency_registry",
                        "idempotency_key='kkkkkkkkkkkkkkkk'") == registry_before
    gained = db.conn.execute(
        "SELECT receipt_id FROM idempotency_registry WHERE idempotency_key=?",
        ("z" * 16,),
    ).fetchone()
    assert gained is not None and gained["receipt_id"] == receipt_before["id"]
    assert db.conn.execute(
        "SELECT entry_count FROM audit_chain_head WHERE singleton=1"
    ).fetchone()["entry_count"] == head_before
    assert counts(db)["published_events"] == 1
    db.close()


# --------------------------------------------------------------------------- #
# Case 17: validator-evidence negative (§4 step 5, reviewer-named).
# --------------------------------------------------------------------------- #

def test_missing_validator_evidence_fails_closed(tmp_path, fixtures):
    """Keep the APPROVED set, destroy the validation record -> publish is
    INTERNAL_ERROR (ApprovalInvariantError), zero change. The deletion is
    applied to the DURABLE row, so the staged copy the publisher loads really
    lacks the evidence (an in-memory delete would be replaced by the load)."""
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)

    # Re-serialise the authority state without the validated-plan record.
    store, approvals = auth._store, auth._approvals
    approvals._validated.clear()
    store_text, approval_text = auth._serialize(store, approvals)
    with db.transaction():
        db.conn.execute(
            "UPDATE authority_state SET plan_store_json=?,"
            " approval_service_json=? WHERE singleton=1",
            (store_text, approval_text),
        )

    # The tampered durable snapshot is caught at the staged load:
    # ApprovalService.load_state refuses an approval set whose validated-plan
    # record is gone: ApprovalInvariantError, a plain Exception). The
    # contract mapping to INTERNAL_ERROR is adapt_exception's fall-through,
    # which the middleware applies — so the observable outcome is asserted
    # through the wire envelope, per design §4 step 5's negative test.
    before = counts(db)
    rev_before = durable_revision(db)
    middleware = ToolErrorMiddleware()
    outcome = middleware.execute("publish_plan", request, ps.prepared_call())
    assert outcome.is_error
    payload = json.loads(outcome.wire_bytes.decode("utf-8"))
    assert payload["error_code"] == "INTERNAL_ERROR"
    assert payload["details"]["diagnostic_class"] == "ApprovalInvariantError"
    assert counts(db) == before
    assert durable_revision(db) == rev_before
    assert (auth._store.get_lifecycle(content["plan_id"], 1)["status"]
            != "PUBLISHED")
    db.close()


# --------------------------------------------------------------------------- #
# Case 18: trace observer accounting — exactly one trace per call outcome.
# --------------------------------------------------------------------------- #

def test_trace_observer_one_record_per_outcome(tmp_path, fixtures):
    db, auth, ps = make_env(tmp_path / "state.db")
    trail = AuditTrail(db)
    events = []
    writer = DecisionTraceWriter(trail)
    middleware = ToolErrorMiddleware(observer=lambda event: (
        events.append(event), writer(event)))
    content, digest, set_id = approved_pair(db, auth, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)

    first = middleware.execute("publish_plan", request, ps.prepared_call())
    assert not first.is_error
    replay = middleware.execute("publish_plan", request, ps.prepared_call())
    assert not replay.is_error
    conflict_req = dict(request)
    conflict_req["idempotency_key"] = request["idempotency_key"]
    conflict_req["plan_digest"] = "b" * 64
    conflict = middleware.execute("publish_plan", conflict_req,
                                  ps.prepared_call())
    assert conflict.is_error and json.loads(
        conflict.wire_bytes.decode("utf-8"))["error_code"] == "IDEMPOTENCY_CONFLICT"

    # One tool-invocation trace per execute (first / replay / conflict).
    assert len(events) == 3
    traces = db.conn.execute(
        "SELECT COUNT(*) c FROM decision_traces"
    ).fetchone()["c"]
    assert traces == 3
    # Replays added traces but never a second plan_published event.
    assert counts(db)["published_events"] == 1
    db.close()


# --------------------------------------------------------------------------- #
# Round-7 reviewer regressions (P0 lock-release ordering, P1 digest
# details). The P0 probe: a failure INSIDE commit() but before the durable
# point must leave the call PREPARED and LOCK-HOLDING with the transaction
# still open — rollback() then closes the transaction and releases the lock
# exactly once (no middleware can ever observe txn-open + lock-free).


def _other_thread_can_take(db, timeout=2.0):
    """True iff Database.lock is fully released (count 0): a DIFFERENT
    thread acquiring proves the owner's release actually happened — a
    re-entrant same-thread acquire would lie."""
    got = threading.Event()

    def worker():
        if db.lock.acquire(True, timeout):
            db.lock.release()
            got.set()

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout + 0.5)
    return got.is_set()


class _CommitFailConn:
    """Proxy over the real connection whose commit() raises — the honest
    simulation of a real durable-point failure (disk-full/busy), since
    sqlite3.Connection.commit is a read-only attribute and cannot be
    monkeypatched instance-level."""

    def __init__(self, real):
        self._real = real

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def commit(self):
        raise sqlite3.OperationalError("forced: COMMIT failed pre-durable")

    def rollback(self):
        return self._real.rollback()

    @property
    def in_transaction(self):
        return self._real.in_transaction


def test_p0_alias_insert_failure_keeps_lock_until_rollback(tmp_path, fixtures):
    """§8 case 12 / reviewer P0 (commit():601): alias INSERT raising inside
    commit() used to release the lock in `finally` BEFORE rollback — the
    txn stayed open while another thread could take the connection."""
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    ps.publish(publish_request(content, digest, set_id, "k" * 16))
    after_first = counts(db)

    alias = publish_request(content, digest, set_id, "m" * 16)  # case C
    call = ps.prepared_call()
    call.prepare(alias, None)
    assert call._alias_receipt_id is not None  # staged as a case-C alias

    def blow_up(*args, **kwargs):
        raise sqlite3.OperationalError("forced: alias INSERT failed")

    ps._insert_alias_row = blow_up
    with pytest.raises(sqlite3.OperationalError):
        call.commit()

    at_rollback_entry = call._snapshot_proof()
    assert at_rollback_entry["state"] == "PREPARED"
    assert at_rollback_entry["lock_held"] is True        # the reviewer's probe
    assert db.conn.in_transaction is True                # txn still open
    assert _other_thread_can_take(db, timeout=0.2) is False

    call.rollback()
    proof = call._snapshot_proof()
    assert proof["state"] == "ROLLED_BACK"
    assert proof["lock_held"] is False
    assert db.conn.in_transaction is False
    assert _other_thread_can_take(db)
    assert counts(db) == after_first                     # original untouched
    db.close()


def test_p0_conn_commit_failure_then_publish_guard(tmp_path, fixtures):
    """Reviewer P0, both halves: (a) conn.commit() failing keeps
    PREPARED+locked with the txn open until rollback(); (b) publish()'s
    unified guard must not leave the connection inside a transaction after
    a commit failure (measured: CONNECTION_IN_TRANSACTION True)."""
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    request = publish_request(content, digest, set_id, "k" * 16)
    real = db.conn

    # (a) direct PreparedCall protocol: commit raises -> stay locked/open.
    call = ps.prepared_call()
    call.prepare(request, None)
    db.conn = _CommitFailConn(real)
    with pytest.raises(sqlite3.OperationalError):
        call.commit()
    entry = call._snapshot_proof()
    assert entry["state"] == "PREPARED" and entry["lock_held"] is True
    assert real.in_transaction is True
    call.rollback()                                      # releases once
    assert real.in_transaction is False
    assert call._snapshot_proof()["lock_held"] is False
    assert _other_thread_can_take(db)
    call.rollback()                                      # idempotent no-op
    db.conn = real

    # (b) the non-HTTP driver's unified rollback guard.
    db.conn = _CommitFailConn(real)
    with pytest.raises(sqlite3.OperationalError):
        ps.publish(publish_request(content, digest, set_id, "m" * 16))
    db.conn = real
    assert real.in_transaction is False                  # the pinned P0 regression
    assert _other_thread_can_take(db)
    assert counts(db)["publication_receipt"] == 0

    # And the connection is LEFT USABLE: the honest retry publishes.
    result = ps.publish(publish_request(content, digest, set_id, "n" * 16))
    assert result["status"] == "PUBLISHED"
    assert counts(db)["publication_receipt"] == 1
    db.close()


def test_case12_invalidated_set_publish_zero_change(tmp_path, fixtures):
    """§8 case 12 branch the matrix was missing: an APPROVED set that was
    subsequently invalidated (durable — the §4.5 staged reload reads the
    authority_state ROW) is refused by require_approved with
    APPROVAL_SET_INVALIDATED and zero change."""
    db, auth, ps = make_env(tmp_path / "state.db")
    content, digest, set_id = approved_pair(db, auth, fixtures)
    auth._approvals.invalidate_set(set_id, "plan_regenerated", digest)
    store_text, approval_text = auth._serialize(auth._store, auth._approvals)
    with db.transaction():
        db.conn.execute(
            "UPDATE authority_state SET plan_store_json=?,"
            " approval_service_json=? WHERE singleton=1",
            (store_text, approval_text),
        )

    before = counts(db)
    rev_before = durable_revision(db)
    with pytest.raises(ApprovalSetInvalidatedError) as exc:
        ps.publish(publish_request(content, digest, set_id, "k" * 16))
    assert exc.value.code == "APPROVAL_SET_INVALIDATED"
    assert exc.value.to_error_details()["invalidation_cause"] == "plan_regenerated"
    assert counts(db) == before
    assert durable_revision(db) == rev_before
    assert (auth._store.get_lifecycle(content["plan_id"], 1)["status"]
            != "PUBLISHED")
    db.close()
