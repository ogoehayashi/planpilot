"""Server-owned clock for every authoritative timestamp (G1, 2026-09-19).

The contract makes expiry server-owned (security_controls.expiry_is_server_owned)
and its clamping rule is evaluated against ``server_now``. That means the
timestamp source is a security boundary: a caller who can supply their own
"now" can walk an expired plan back into life and dodge APPROVAL_EXPIRED.

So no request path may carry a timestamp. Every server-side timestamp —
lifecycle, approval, audit chain, decision trace, security event, factory
state — flows through ONE Clock object injected at the composition root
(Server wires it into Database and RuntimeAuthority; G1.0.1 closed the
wall-clock leak that made audit stamps disagree with scenario stamps):

- ``WallClock``  — the fail-safe default. Real machine time in Asia/Singapore
  (+08:00), matching persistence.now(). Production deployment is wall mode.
- ``FixedClock`` — frozen deterministic time for unit tests and replay only.
- ``ScenarioClock`` — declared demo clock: anchored at a dataset-derived
  scenario date but ADVANCING with real elapsed time (downtime included), so
  approvals still expire if a demo runs long, while the calendar age of the
  fixed dataset stays inside its horizon on any host date. G1.0.2: when bound
  to a Database it persists a clock session — restart NEVER rewinds issued
  scenario time. Never the default; only via PLANPILOT_CLOCK_MODE=scenario
  plus an explicit PLANPILOT_SCENARIO_NOW anchor, and it refuses to bind a
  public address without an explicit override.

Switching the clock never rewrites stored plan data: digests are immutable
and computed over content only, so demo/replay/fixed timestamps cannot change
an installed plan.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Clock:
    """Abstract server-owned clock."""

    kind = "wall"
    scenario: dict | None = None

    def now(self) -> str:
        raise NotImplementedError

    def elapsed(self) -> float:
        """Seconds this clock has been running since boot (monotonic)."""
        raise NotImplementedError

    def status(self) -> dict:
        return {"kind": self.kind, "now": self.now(), "scenario": self.scenario,
                "uptime_seconds": round(self.elapsed(), 3)}


class WallClock(Clock):
    """Real machine time, rendered in Asia/Singapore like every stored stamp."""

    kind = "wall"

    def __init__(self):
        self._boot = time.monotonic()

    def now(self) -> str:
        return datetime.now(SGT).isoformat()

    def elapsed(self) -> float:
        return time.monotonic() - self._boot


class FixedClock(Clock):
    """Frozen deterministic clock for tests and replay; the operator injects it.

    Not for live demos: it never advances, so pending approvals would hang
    open forever. Use ScenarioClock for a running demo process.
    """

    kind = "fixed"

    def __init__(self, iso: str, scenario: dict | None = None):
        # Re-serialise via isoformat() so the stamp always carries the 'T'
        # separator the contract date-time format demands (str(datetime)
        # would emit a space separator).
        self._now = parse_iso(iso).isoformat(timespec="seconds")
        self.scenario = scenario

    def advance(self, seconds: float) -> None:
        self._now = (parse_iso(self._now) + timedelta(seconds=seconds)
                     ).isoformat(timespec="seconds")

    def now(self) -> str:
        return self._now

    def elapsed(self) -> float:
        return 0.0


class ScenarioClock(Clock):
    """Advancing demo clock: scenario anchor + real elapsed, restart-safe.

    Anchored inside the fixed dataset's horizon so a demo works on any host
    date, yet expiry still works: wait 24h of real time and pending approvals
    genuinely expire. The UI and /clock label this as scenario time; the
    horizon itself is derived from the loaded dataset — never hand-copied.

    G1.0.2 (review P0): a restart must NOT rewind server time. The naive
    "re-anchor on boot" design let repeated restarts extend every pending
    approval window (audit stamps also went backwards). When attached to a
    Database the clock persists a session row (clock_session) and on reopen
    resumes from

        max(scenario_anchor
            + (real wall now - real_wall_started_at),   # downtime counts
            last_issued_scenario_time)                  # crash/NTP safety

    so issued scenario time is monotonic non-decreasing for the life of the
    database file. last_issued is written at seconds granularity (the stamp
    resolution), so a hard crash can lose at most 1 second of elapsed time —
    never hours. An unattached clock keeps the old process-local behaviour
    for unit tests. Re-running the same file against a DIFFERENT anchor
    fails closed instead of quietly mixing two scenario timelines.
    """

    kind = "scenario"

    def __init__(self, iso: str, scenario: dict | None = None):
        self._anchor = parse_iso(iso)
        self._base = self._anchor
        self.scenario = scenario
        self.session = None
        self._db = None
        self._saved = None
        self._boot = time.monotonic()

    def attach_database(self, db) -> None:
        """Bind this clock to its durable session (composition root only)."""
        if self._db is not None:
            if self._db is db:
                return
            raise RuntimeError(
                "scenario clock is already attached to another database")
        anchor_iso = self._anchor.isoformat(timespec="seconds")
        wall_now = datetime.now(SGT).isoformat(timespec="seconds")
        with db.lock:
            row = db.conn.execute(
                "SELECT scenario_anchor, real_wall_started_at,"
                " last_issued_scenario_time FROM clock_session WHERE singleton=1"
            ).fetchone()
            if row is None:
                db.conn.execute(
                    "INSERT INTO clock_session VALUES(1,?,?,?)",
                    (anchor_iso, wall_now, anchor_iso))
                self._saved = anchor_iso
                self.session = {"anchor": anchor_iso,
                                "real_wall_started_at": wall_now,
                                "restored": False}
            else:
                if row["scenario_anchor"] != anchor_iso:
                    raise ValueError(
                        f"this database's clock session was created for anchor "
                        f"{row['scenario_anchor']!r}; refusing to re-anchor it "
                        f"to {anchor_iso!r} (use a fresh database file)")
                downtime = parse_iso(wall_now) - parse_iso(row["real_wall_started_at"])
                candidate = self._anchor + downtime
                base = max(candidate, parse_iso(row["last_issued_scenario_time"]))
                self._base = base
                self._boot = time.monotonic()
                self._saved = row["last_issued_scenario_time"]
                self.session = {"anchor": anchor_iso,
                                "real_wall_started_at": row["real_wall_started_at"],
                                "restored": True}
        self._db = db

    def now(self) -> str:
        stamp = (self._base
                 + timedelta(seconds=time.monotonic() - self._boot)
                 ).isoformat(timespec="seconds")
        # Return the database high-water, not the local candidate: under the
        # ThreadingHTTPServer a thread can compute an older stamp, block on
        # the lock, and wake after a newer one persisted. Callers must never
        # observe issued time going backwards (G1.0.2 follow-up).
        return self._persist(stamp)

    def _persist(self, stamp: str) -> str:
        # High-water update in ONE statement (G1.0.2 re-review): the CASE
        # keeps max(row, stamp) atomically and RETURNING hands back the
        # row's value, so a caller whose UPDATE touched zero rows —
        # because another connection or process already advanced it —
        # still receives the database truth, never its own stale stamp.
        if self._db is None:
            return stamp
        with self._db.lock:
            row = self._db.conn.execute(
                "UPDATE clock_session"
                " SET last_issued_scenario_time = ("
                "   CASE WHEN last_issued_scenario_time < ?"
                "        THEN ? ELSE last_issued_scenario_time END)"
                " WHERE singleton = 1"
                " RETURNING last_issued_scenario_time",
                (stamp, stamp)).fetchone()
            if row is None:
                # G4 A1 (design §2): the anchor row is GONE. Honest flow
                # cannot reach here — attach_database INSERTs the singleton
                # BEFORE binding _db — so this is tamper (delete bypassing
                # the trigger, or a hand-carved file). Fail CLOSED: never
                # hand back an un-persisted stamp as if it were durable.
                raise RuntimeError(
                    "clock session row missing — refusing to issue "
                    "un-persisted scenario time")
            self._saved = row[0]
            return self._saved

    def elapsed(self) -> float:
        return time.monotonic() - self._boot

    def status(self) -> dict:
        out = super().status()
        if self.session is not None:
            out["session"] = dict(self.session)
        return out


def clock_from_env(environ, host: str) -> Clock:
    """Fail-safe clock policy (G1.0.1): absent/invalid config means WALL.

    PLANPILOT_CLOCK_MODE=wall      -> WallClock (default, production)
    PLANPILOT_CLOCK_MODE=scenario  -> advancing ScenarioClock (local demo);
      refuses non-loopback binds unless PLANPILOT_ALLOW_PUBLIC_SCENARIO=1,
      and prints nothing that could be mistaken for production time.
    Unknown values fail startup — the old default-ON scenario meant a
    Lightsail/Docker host that forgot the env var ran on a fake 2026-09-14
    clock and could approve stale plans.
    """
    mode = str(environ.get("PLANPILOT_CLOCK_MODE") or "wall").strip().lower()
    if mode == "wall":
        return WallClock()
    if mode == "scenario":
        if (host not in ("127.0.0.1", "localhost", "::1")
                and environ.get("PLANPILOT_ALLOW_PUBLIC_SCENARIO") != "1"):
            raise ValueError(
                f"refusing scenario clock on public bind {host!r}: run "
                "PLANPILOT_CLOCK_MODE=wall, or set "
                "PLANPILOT_ALLOW_PUBLIC_SCENARIO=1 to acknowledge the risk")
        # G1.0.2 (review P1-3): NO silent hardcoded anchor. The scenario
        # anchor must be derived from the dataset actually being served
        # (start_local.ps1 reads planning_start) and passed explicitly;
        # swapping datasets can no longer drift the clock behind the data.
        raw_anchor = str(environ.get("PLANPILOT_SCENARIO_NOW") or "").strip()
        if not raw_anchor:
            raise ValueError(
                "PLANPILOT_CLOCK_MODE=scenario requires an explicit "
                "PLANPILOT_SCENARIO_NOW anchor derived from the dataset "
                "(planning_start); refusing a silent hardcoded date")
        try:
            anchor = parse_iso(raw_anchor)
        except ValueError as exc:
            raise ValueError(
                f"PLANPILOT_SCENARIO_NOW={raw_anchor!r} is not a valid "
                "ISO-8601 timestamp") from exc
        if anchor.utcoffset() is None:
            raise ValueError(
                f"PLANPILOT_SCENARIO_NOW={raw_anchor!r} has no timezone; "
                "an explicit +08:00 Singapore offset is required")
        if anchor.utcoffset() != timedelta(hours=8):
            raise ValueError(
                f"PLANPILOT_SCENARIO_NOW={raw_anchor!r} must be +08:00; "
                "every stored stamp in this system is Asia/Singapore")
        return ScenarioClock(
            anchor.isoformat(timespec="seconds"),
            scenario={
                "dataset": environ.get("PLANPILOT_SCENARIO_DATASET",
                                       "factory_demo_v18.json"),
                "note": "Server scenario clock; an expired window requires a regenerated plan; old plans must not be revived.",
            },
        )
    raise ValueError(
        f"unknown PLANPILOT_CLOCK_MODE={mode!r}; expected 'wall' or 'scenario'")


DEFAULT_CLOCK = WallClock()
