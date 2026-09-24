"""G4 Phase 3 — health split (design §4): /health unchanged + live/ready/deep.

Everything runs over REAL HTTP against a REAL Server with a REAL
sqlite file; the lock tests take REAL BEGIN IMMEDIATE writers.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from planpilot.clock import ScenarioClock, WallClock
from planpilot.persistence import Database
from planpilot.security import issue_token

ROOT = Path(__file__).resolve().parents[2]
SECRET = "g4-p3-health-split-secret-at-least-32-chars"

# no-verify-across-imports: test_runtime_authority is a sibling unit module
# and p2-5 already imports it the same way.
from unit.test_runtime_authority import clock


def _api_module():
    spec = importlib.util.spec_from_file_location("g4p3_api", ROOT / "tools/api_server.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def _get(server, path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(f"http://127.0.0.1:{server.server_port}{path}", headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture()
def live_server(tmp_path):
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "state.db", clock=fixed)
    server = api.Server(("127.0.0.1", 0), db, SECRET, ROOT / "data", clock=fixed)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield api, server, db
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    db.close()


# ------------------------------------------------------------- 3.0 compat --

def test_legacy_health_body_is_verbatim_compatible(live_server):
    _, server, _ = live_server
    status, body = _get(server, "/health")
    assert status == 200
    # EXACTLY the pre-G4 body: no probe field, no new keys.
    assert body == {"status": "ok", "service": "planpilot"}


# -------------------------------------------------------------- 3.1 live --

def test_live_answers_with_db_locked_and_never_touches_clock(live_server):
    _, server, db = live_server
    calls = []
    scenario_now, wall_now = ScenarioClock.now, WallClock.now
    ScenarioClock.now = lambda self: (calls.append("s"), scenario_now(self))[1]
    WallClock.now = lambda self: (calls.append("w"), wall_now(self))[1]
    try:
        holder = sqlite3.connect(str(db.path), timeout=10)
        holder.execute("BEGIN IMMEDIATE")  # real writer lock held out-of-band
        try:
            started = time.monotonic()
            status, body = _get(server, "/health/live")
            assert status == 200
            assert body == {"status": "ok", "service": "planpilot", "probe": "live"}
            # liveness must not queue on the write lock
            assert time.monotonic() - started < 1.0
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert calls == []  # zero clock.now() on the live path
    finally:
        ScenarioClock.now, WallClock.now = scenario_now, wall_now


# ------------------------------------------------------------- 3.2 ready --

def test_ready_503_under_real_writer_lock_then_200_after_release(live_server):
    _, server, db = live_server
    holder = sqlite3.connect(str(db.path), timeout=10)
    holder.execute("BEGIN IMMEDIATE")
    try:
        status, body = _get(server, "/health/ready")
        assert status == 503
        assert body["status"] == "not_ready"
        assert body["probe"] == "ready"
        assert "busy" in body["db"].lower() or "locked" in body["db"].lower()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    status, body = _get(server, "/health/ready")
    assert status == 200
    assert body == {"status": "ok", "service": "planpilot",
                    "probe": "ready", "db": "ok"}
    # NO checked_at ever (design §3: readiness must not read the clock)
    assert "checked_at" not in body


def test_ready_never_calls_clock_now(live_server):
    _, server, _ = live_server
    calls = []
    scenario_now, wall_now = ScenarioClock.now, WallClock.now
    ScenarioClock.now = lambda self: (calls.append("s"), scenario_now(self))[1]
    WallClock.now = lambda self: (calls.append("w"), wall_now(self))[1]
    try:
        for _ in range(3):
            status, _body = _get(server, "/health/ready")
            assert status == 200
        assert calls == []
    finally:
        ScenarioClock.now, WallClock.now = scenario_now, wall_now


def test_ready_503_on_readonly_file_never_false_green(live_server):
    """design §4 falsifier (b), empirically pinned THIS batch on Windows:
    sqlite ACCEPTS `BEGIN IMMEDIATE` on a chmod-0o444 database (delete
    journal AND WAL both) and only denies the WRITE — a reservation-only
    probe would report 200 on a DB that cannot durably serve a single
    change. The in-transaction CREATE falsifier must turn that state
    into a 503."""
    import stat
    _, server, db = live_server
    mode = os.stat(db.path).st_mode
    os.chmod(db.path, stat.S_IREAD)
    try:
        status, body = _get(server, "/health/ready")
        assert status == 503
        assert "readonly" in body["db"].lower()
    finally:
        os.chmod(db.path, mode)
    # restored: ready again, and the denial left zero residue behind
    status, _ = _get(server, "/health/ready")
    assert status == 200


def test_ready_success_leaves_zero_residue(live_server):
    """§4(c) made concrete: the probe writes NOTHING it could forget —
    its in-txn CREATE is rolled back, sqlite_master stays clean, and no
    business row moves."""
    _, server, db = live_server
    with db.lock:
        before_names = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master")}
        audit_before = db.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_chain").fetchone()["n"]
    for _ in range(2):
        assert _get(server, "/health/ready")[0] == 200
    probe = sqlite3.connect(str(db.path), timeout=5)
    try:
        after_names = {r[0] for r in probe.execute(
            "SELECT name FROM sqlite_master")}
        assert after_names == before_names          # zero residue, exact set
        assert probe.execute(
            "SELECT COUNT(*) FROM audit_chain").fetchone()[0] == audit_before
    finally:
        probe.close()


def test_ready_budget_bounded_by_snapshot_timeout(tmp_path):
    """locked DB with a 600ms budget: the request must fail-closed INSIDE
    the budget tolerance — proof the timeout is actually honoured, not a
    hang and not an instant lie."""
    api = _api_module()
    fixed = clock()
    db = Database(tmp_path / "b.db", clock=fixed)
    server = api.Server(("127.0.0.1", 0), db, SECRET, ROOT / "data",
                        clock=fixed, ready_timeout_ms=600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        holder = sqlite3.connect(str(db.path), timeout=10)
        holder.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            status, _ = _get(server, "/health/ready")
            elapsed = time.monotonic() - started
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert status == 503
        assert 0.4 <= elapsed <= 2.5  # budget honoured with connect-time tolerance
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()


def test_ready_refuses_corrupt_file_not_false_green(tmp_path):
    """A file that CANNOT really serve writes (corrupt header) must be
    503 — the probe claims RW+writer-reservation only, and a read-only
    success would be exactly the false green this seam exists to catch."""
    api = _api_module()
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"not a sqlite file" + b"\x00" * 4096)
    # The server needs a real DB; point readiness at the corrupt sibling.
    fixed = clock()          # ONE instance: db and server share the clock
    db = Database(tmp_path / "ok.db", clock=fixed)
    server = api.Server(("127.0.0.1", 0), db, SECRET, ROOT / "data",
                        clock=fixed)
    original_path = server.db.path
    server.db.path = str(bad)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(server, "/health/ready")
        assert status == 503
        assert "notadb" in body["db"].lower() or "error" in body["db"].lower()
    finally:
        server.db.path = original_path
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()


# -------------------------------------------------------------- 3.3 deep --

def test_deep_requires_plan_scope_403_convention(live_server):
    _, server, _ = live_server
    status, _ = _get(server, "/health/deep")
    assert status == 403                      # repo convention, no invented 401
    # wrong secret (signature fail) — the ONLY honest 403 shape available:
    # security.py ROLES grants `plan` to BOTH planner and manager, so
    # scope-refusal-by-role cannot be demonstrated; pin that fact here
    # rather than pretending otherwise.
    assert "plan" in issue_perms("manager")
    status, _ = _get(server, "/health/deep",
                     issue_token("x", "manager", SECRET + "-wrong"))
    assert status == 403


def issue_perms(role):
    from planpilot.security import ROLES
    return ROLES[role]


def test_deep_200_always_even_when_checks_fail(live_server):
    _, server, db = live_server
    # real audit history so the chain/head checks have teeth
    db.audit("PLAN-DEEP-1", "tester", "g4.deep.test", {"probe": "seed"})
    token = issue_token("auditor", "planner", SECRET)
    status, body = _get(server, "/health/deep", token)
    assert status == 200
    assert body["overall_ok"] is True
    assert body["probe"] == "deep"
    assert "status" not in body               # never mimics readiness shape
    checks = body["checks"]
    assert checks["audit_chain_ok"] is True
    assert checks["audit_head"]["entry_count"] >= 1
    # Phase 4 has not shipped: manifest lookup must answer honestly,
    # NOT scan newest-file mtimes.
    assert checks["backup"] == {"state": "no_verified_manifest"}
    # Now break the chain out-of-band; deep must STILL be 200, with
    # overall_ok False (diagnostics can never flip container gates).
    # audit_chain has UPDATE/DELETE guards, so the out-of-band path is
    # DROP-then-DELETE (same privileged-file shape Phase 1 pinned).
    with db.lock:
        db.conn.execute("DROP TRIGGER audit_chain_no_delete")
        db.conn.execute("DELETE FROM audit_chain WHERE id=1")
        db.conn.commit()
    status, body = _get(server, "/health/deep", token)
    assert status == 200
    assert body["overall_ok"] is False
    assert body["checks"]["audit_chain_ok"] is False


# ---------------------------------------------------- C4 assembly for Ph4 --

def test_probe_is_isolated_function_for_os_lock_insertion():
    """Phase 4 must be able to slot the OS lock/recovery gate between
    preflight and Database() WITHOUT unwrapping Server.__init__ again:
    the readiness protocol lives in its own seam (tested + mutated by
    negctl only there), and startup assembly order is main() ->
    snapshot -> parse/preflight -> (Ph4: lock/gate) -> Clock -> Database
    -> Server. Structural proof: _probe_ready exists standalone and
    Server.__init__ performs zero I/O readiness probing."""
    import inspect
    api = _api_module()
    src = inspect.getsource(api.Handler._probe_ready)
    assert "BEGIN IMMEDIATE" in src and "ROLLBACK" in src
    init_src = inspect.getsource(api.Server.__init__)
    assert "BEGIN IMMEDIATE" not in init_src
    main_src = inspect.getsource(api.main)
    order = [main_src.index(m) for m in (
        "dict(os.environ)", "startup_or_die", "clock_from_env",
        "Database(")]
    assert order == sorted(order)   # snapshot -> parse/preflight -> clock -> DB
    # Phase 4 has NOT run yet — say so in CODE, not prose: strip comment
    # lines (they legitimately describe the future gate) then assert no
    # statement before the preflight call acquires anything.
    code_before = "\n".join(
        ln for ln in main_src.split("startup_or_die")[0].splitlines()
        if not ln.strip().startswith("#"))
    assert "acquire(" not in code_before
    assert "os_lock" not in code_before


# ------------------------------------------------ automation uses ready --

def test_no_automation_targets_deep(live_server):
    """Deep can never be a gate: no repo AUTOMATION (deploy files, shell
    scripts, gate tooling) references /health/deep. api_server.py itself
    IMPLEMENTS the route and is excluded — the invariant is that nothing
    CONSUMES deep as a readiness/liveness target. (Dockerfile/compose
    migration to /health/ready is Phase 5 scope.)"""
    offenders = []
    targets = ([f for f in (ROOT / "tools").rglob("*.py")
                if f.name != "api_server.py"]
               + list(ROOT.glob("*.py")) + list(ROOT.glob("Dockerfile*"))
               + list(ROOT.glob("compose*.y*ml")) + list(ROOT.glob("*.sh")))
    for f in targets:
        if f.is_file() and "/health/deep" in f.read_text(
                encoding="utf-8", errors="replace"):
            offenders.append(str(f.relative_to(ROOT)))
    assert offenders == [], f"automation must never target deep: {offenders}"
