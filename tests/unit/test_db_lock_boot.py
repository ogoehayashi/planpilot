"""G4 4.4 (R1 + BOOT ORDER): the OS DB lock IS the safety mechanism.

Proves: acquire/release roundtrip with advisory pid hint, CROSS-
PROCESS contention, release-on-process-death (stale-free by
construction), and main()'s boot order — a denied lock means ZERO
Clock/Database/Server construction and no DB file, while shutdown
runs HTTP -> DB close -> lock release LAST.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from planpilot.db_lock import (DbLockHeld, acquire_server_lock,
                               lock_path_for, probe_server_lock)

GOOD_SECRET = "s" * 40

HOLDER = (
    "import os, sys, time\n"
    "sys.path.insert(0, 'src')\n"
    "from planpilot.db_lock import acquire_server_lock\n"
    "h = acquire_server_lock(sys.argv[1])\n"
    "print('LOCKED', os.getpid(), flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
    "h.release()\n")


def _kill_tree(p):
    """The venv python.exe on Windows is a trampoline that execs the
    real interpreter as a CHILD: bare p.kill() would orphan the actual
    lock holder. taskkill /F /T ends the whole tree; wait() returns
    only once the pid is gone, so handles (and the OS lock) are
    released by then."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.wait(timeout=10)
    else:
        p.kill()
        p.wait(timeout=10)


@pytest.fixture
def holder():
    procs = []

    def _start(db_path, secs=5.0):
        p = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(db_path), str(secs)],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(p)
        parts = p.stdout.readline().split()
        assert parts and parts[0] == "LOCKED", p.stderr.read()
        # the CHILD's own getpid() — the trampoline's Popen pid is not
        # the process that wrote the hint.
        return p, int(parts[1])

    yield _start
    for p in procs:
        try:
            _kill_tree(p)
        except Exception:
            pass


def test_roundtrip_and_advisory_hint(tmp_path):
    db = tmp_path / "planpilot.db"
    h = acquire_server_lock(db)
    assert h.held
    lk = lock_path_for(db)
    assert h.path == lk and lk.exists()
    hint = Path(str(lk) + ".pid")
    assert hint.read_text(encoding="utf-8") == str(os.getpid())
    h.release()
    assert not h.held
    assert not hint.exists()        # hint is written ONLY while held
    h.release()                     # idempotent
    acquire_server_lock(db).release()


def test_cross_process_contention_and_death_release(tmp_path, holder):
    db = tmp_path / "planpilot.db"
    p, holder_pid = holder(db)
    with pytest.raises(DbLockHeld) as ei:
        acquire_server_lock(db)
    msg = str(ei.value)
    assert "server appears to be running" in msg
    assert str(holder_pid) in msg   # advisory hint, printed not trusted
    _kill_tree(p)
    # stale-free BY DESIGN: the OS released it when the holder died.
    acquire_server_lock(db).release()


def test_probe_server_lock_free_and_held(tmp_path, holder):
    db = tmp_path / "planpilot.db"
    probe_server_lock(db)           # free -> no raise
    p, _pid = holder(db)
    with pytest.raises(DbLockHeld):
        probe_server_lock(db)
    _kill_tree(p)


# --- main() BOOT ORDER (design §5, round-4 P0-2) --------------------

def _boot_env(tmp_path, db_path):
    return {
        "PLANPILOT_ENV": "development",
        "PLANPILOT_AUTH_SECRET": GOOD_SECRET,
        "PLANPILOT_DB": str(db_path),
        "PLANPILOT_FACTORY_ROOT": str(tmp_path),
        "PLANPILOT_PORT": "0",
    }


def _spy_boot(api_server, monkeypatch, events):
    """Replace the three constructors + lock release with recorders so
    the test can pin the EXACT call order of a boot attempt."""
    from planpilot.persistence import Database as RealDatabase
    RealServer = api_server.Server

    def _clock(snapshot, host):
        events.append("clock")
        return api_server.WallClock()

    class DbSpy:
        def __init__(self, path, clock=None):
            events.append("database")
            self.real = RealDatabase(path, clock=clock)

        def __getattr__(self, name):
            # delegate everything the real Database offers (bind_clock,
            # the schema probe, ...) so Server.__init__ runs for real —
            # this spy only OBSERVES construction and close order
            return getattr(self.real, name)

        def close(self):
            events.append("db-close")
            self.real.close()

    class ServerSpy(RealServer):
        def serve_forever(self):
            events.append("serve")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("http-stop")
            super().server_close()

    class LockProxy:
        """main() only ever calls .release() on the handle — wrap it so
        the shutdown order is pinned by an OBSERVED event, not by
        reading the finally block."""
        def __init__(self, real):
            self._real = real

        def release(self):
            events.append("lock-release")
            self._real.release()

    real_acquire = api_server.acquire_server_lock

    def _acquire(p):
        events.append("lock")
        return LockProxy(real_acquire(p))

    monkeypatch.setattr(api_server, "clock_from_env", _clock)
    monkeypatch.setattr(api_server, "Database", DbSpy)
    monkeypatch.setattr(api_server, "Server", ServerSpy)
    monkeypatch.setattr(api_server, "acquire_server_lock", _acquire)


def _run_main(api_server, monkeypatch, tmp_path, db_path):
    monkeypatch.setattr(api_server.os, "environ", _boot_env(tmp_path, db_path))
    events = []
    _spy_boot(api_server, monkeypatch, events)
    with pytest.raises(KeyboardInterrupt):
        api_server.main()
    return events


def _import_api_server():
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    return importlib.import_module("api_server")


def test_lock_precedes_every_writer(tmp_path, monkeypatch):
    api_server = _import_api_server()
    db = tmp_path / "planpilot.db"
    events = _run_main(api_server, monkeypatch, tmp_path, db)
    assert events.index("lock") < events.index("clock")
    assert events.index("lock") < events.index("database")
    assert events.index("lock") < events.index("serve")


def test_lock_released_last_on_shutdown(tmp_path, monkeypatch):
    """Shutdown mirror: serve -> HTTP stop (inside server_close) ->
    db-close -> lock-release LAST. Pinned on the OBSERVED event list,
    exact order, nothing else."""
    api_server = _import_api_server()
    db = tmp_path / "planpilot.db"
    events = _run_main(api_server, monkeypatch, tmp_path, db)
    assert events == ["lock", "clock", "database", "serve",
                      "http-stop", "db-close", "lock-release"]


def test_denied_lock_zero_construction(tmp_path, monkeypatch, holder):
    api_server = _import_api_server()
    db = tmp_path / "planpilot.db"
    p, _pid = holder(db)
    events = []

    def _clock(snapshot, host):
        events.append("clock")
        return api_server.WallClock()

    class DbSpy:
        def __init__(self, path, clock=None):
            events.append("database")
            raise AssertionError("Database must NEVER be constructed")

    monkeypatch.setattr(api_server, "clock_from_env", _clock)
    monkeypatch.setattr(api_server, "Database", DbSpy)
    monkeypatch.setattr(api_server.os, "environ", _boot_env(tmp_path, db))
    snapshot = sorted(p.name for p in tmp_path.iterdir())  # holder's lock+hint
    with pytest.raises(SystemExit) as ei:
        api_server.main()
    assert "server appears to be running" in str(ei.value)
    assert events == []             # zero Clock, zero Database
    assert not db.exists()          # DB file byte-untouched (never made)
    # design §5 guard: a denied boot must NOT execute ANY DDL side —
    # directory listing identical, no .db/.db-wal/.server.lock new files
    assert sorted(p.name for p in tmp_path.iterdir()) == snapshot
    _kill_tree(p)

