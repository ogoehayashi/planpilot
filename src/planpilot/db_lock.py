"""G4 4.4 (design §5 R1): one cross-process OS lock guarding one database.

The safety mechanism is the OS lock itself — msvcrt.locking(LK_NBLCK)
byte-range lock on Windows, fcntl.flock(LOCK_EX | LOCK_NB) on POSIX.
Both are released by the OPERATING SYSTEM when the holding process
dies, so there is NO stale-lock cleanup to trust: a lock we cannot
take means a live process holds it RIGHT NOW. The pid hint file is an
optional convenience for error messages only — never the mechanism
(design: "pidfile is OPTIONAL HINT ONLY ... never the safety
mechanism").

Lock naming: <db>.server.lock (OS-locked file) plus an advisory
<db>.server.lock.pid hint written ONLY while the lock is held.
"""
from __future__ import annotations

import os
from pathlib import Path

_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl


class DbLockHeld(RuntimeError):
    """Another live process holds the DB lock. Non-blocking by design:
    both the server entry point and tools/restore_database.py try once
    and fail immediately — 'server appears to be running'."""


def lock_path_for(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".server.lock")


def _pid_hint_path(lk: Path) -> Path:
    return Path(str(lk) + ".pid")


class DbLockHandle:
    """Holds the OS lock for its lifetime; release() is idempotent."""

    __slots__ = ("path", "_fd")

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if _IS_WINDOWS:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # we are unlocking our own descriptor; losing it to
                   # process teardown must not mask shutdown
        finally:
            os.close(fd)
        hint = _pid_hint_path(self.path)
        try:
            hint.unlink()
        except OSError:
            pass  # advisory only


def _try_lock(fd: int) -> None:
    if _IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire_server_lock(db_path: str | Path) -> DbLockHandle:
    """NON-BLOCKING take of <db>.server.lock; raises DbLockHeld on the
    spot if a live process holds it. Held for life: the server acquires
    at startup BEFORE any Database/Clock construction (boot order) and
    releases LAST, after HTTP stops and the DB closes."""
    lk = lock_path_for(db_path)
    lk.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lk), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _try_lock(fd)
    except OSError as exc:
        os.close(fd)
        hint = "unknown"
        try:
            h = _pid_hint_path(lk).read_text(encoding="utf-8").strip()
            hint = h or "unknown"
        except OSError:
            pass
        raise DbLockHeld(
            f"server appears to be running (pid hint {hint}); "
            f"OS DB lock held on {lk.name}") from exc
    try:
        _pid_hint_path(lk).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass  # hint is optional — losing it never releases the lock
    return DbLockHandle(lk, fd)


def probe_server_lock(db_path: str | Path) -> None:
    """Raise DbLockHeld iff the lock is currently taken. Takes and
    instantly releases the lock — no side effect when free."""
    acquire_server_lock(db_path).release()
