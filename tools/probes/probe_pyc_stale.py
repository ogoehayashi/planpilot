#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproduce the stale-bytecode mechanism behind negative-control defect D20.

WHY THIS IS COMMITTED
---------------------
IMPLEMENTATION_NOTES.md (D20) and REVIEW_HANDOFF_IMPLEMENTATION.md §2.4 both
assert that the intermittent false `escaped=1` in the negative control was caused
by CPython serving a STALE `.pyc`, and that the mechanism was "reproduced
deterministically". That claim is only credible if the reviewer can run the
reproduction. It lives here, in the repo, not in a scratch directory outside it.

Run it:  python tools/probes/probe_pyc_stale.py
Expected: the VERDICT block prints REPRODUCED.

THE MECHANISM
-------------
The negative control rewrites plan_store.py once per mutation and spawns a fresh
pytest subprocess each time. CPython validates a cached .pyc against the source's
mtime — TRUNCATED TO WHOLE SECONDS — and its size. So if mutation N and mutation
N+1 are written within the same second AND happen to produce the same file size,
the subprocess for N+1 can load N's bytecode and run the WRONG mutation. That
makes the control's verdict depend on wall-clock timing and byte-length
collisions, i.e. intermittent — which is exactly what was observed: P1-a(2)
reported an escape inside the control but the same mutation passed 7/7 in
isolation.

This reproduces it DETERMINISTICALLY rather than waiting for a collision: it
writes two equal-length sources pinned to the same mtime second and checks which
one a child process actually imports. It touches nothing in the repo — everything
happens in a temp dir that is removed on exit.
"""
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PY = sys.executable
WORK = Path(os.environ.get("TEMP", "/tmp")) / "pyc_stale_probe"


def _reset_workdir():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)


def main() -> int:
    _reset_workdir()

    # Two module bodies, EXACTLY the same byte length, different observable value.
    body_a = 'VALUE = "AAAAAAAA"\n'
    body_b = 'VALUE = "BBBBBBBB"\n'
    assert len(body_a) == len(body_b), "the probe needs equal-length sources"

    mod = WORK / "stalemod.py"
    runner = WORK / "runner.py"
    runner.write_text(
        "import stalemod, sys\n"
        "sys.stdout.write(stalemod.VALUE)\n",
        encoding="utf-8",
    )

    def run_child() -> str:
        p = subprocess.run([PY, str(runner)], cwd=str(WORK), capture_output=True,
                           text=True, encoding="utf-8")
        return p.stdout.strip()

    print("=" * 70)
    print("Control: change the source with a NEW mtime second -> .pyc invalidated")
    print("=" * 70)
    mod.write_text(body_a, encoding="utf-8")
    print(f"  wrote A, child imports: {run_child()}")
    now = time.time()
    os.utime(mod, (now - 10, now - 10))
    mod.write_text(body_b, encoding="utf-8")
    os.utime(mod, (now - 10, now - 10))   # keep the OLD second, same size
    print("  wrote B with an artificially OLDER mtime + same size")
    print(f"  child imports: {run_child()}   (B expected: a different second invalidates)")

    print()
    print("=" * 70)
    print("The hazard: rewrite within the SAME mtime second, SAME size")
    print("=" * 70)
    mod.write_text(body_a, encoding="utf-8")
    t = int(time.time())
    os.utime(mod, (t, t))
    first = run_child()
    print(f"  wrote A at second {t}, child imports: {first}  (.pyc now cached for A)")

    os.utime(mod, (t, t))
    mod.write_text(body_b, encoding="utf-8")
    os.utime(mod, (t, t))   # pin mtime back to the same whole second
    second = run_child()
    print(f"  wrote B at the SAME second {t}, same size, child imports: {second}")

    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    if second == "AAAAAAAA":
        print("  REPRODUCED: the child imported A's STALE bytecode although the file")
        print("  on disk says B. CPython trusted the cached .pyc because mtime(second)")
        print("  and size were unchanged. This is exactly how the negative control can")
        print("  run the wrong mutation and report a false escape.")
        print("  -> the control must clear __pycache__ (or disable bytecode) per mutation.")
        rc = 0
    elif second == "BBBBBBBB":
        print("  NOT reproduced: the child imported B. Either this CPython build")
        print("  invalidates more aggressively, or the .pyc check uses sub-second mtime.")
        print("  The stale-bytecode hypothesis would NOT be the mechanism on this build.")
        rc = 1
    else:
        print(f"  unexpected child output: {second!r}")
        rc = 2

    shutil.rmtree(WORK, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
