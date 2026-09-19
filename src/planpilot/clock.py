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
- ``ScenarioClock`` — declared demo clock pinned at a scenario anchor but
  ADVANCING with real elapsed time since boot: approvals still expire if a
  demo runs long, while the calendar age of the fixed dataset stays inside
  its horizon on any host date. Restart re-anchors (epoch = process boot).
  Never the default; only via PLANPILOT_CLOCK_MODE=scenario, and it refuses
  to bind a public address without an explicit override.

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
    """Advancing demo clock: scenario anchor + real elapsed time since boot.

    Anchored inside the fixed dataset's horizon so a demo works on any host
    date, yet expiry still works: wait 24h of real time and pending approvals
    genuinely expire. The UI and /clock label this as scenario time; the
    horizon itself is derived from the loaded dataset (Server derives it) —
    never hand-copied into clock metadata.
    """

    kind = "scenario"

    def __init__(self, iso: str, scenario: dict | None = None):
        self._anchor = parse_iso(iso)
        self.scenario = scenario
        self._boot = time.monotonic()

    def now(self) -> str:
        return (self._anchor
                + timedelta(seconds=time.monotonic() - self._boot)
                ).isoformat(timespec="seconds")

    def elapsed(self) -> float:
        return time.monotonic() - self._boot


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
        return ScenarioClock(
            environ.get("PLANPILOT_SCENARIO_NOW", "2026-09-14T08:00:00+08:00"),
            scenario={
                "dataset": environ.get("PLANPILOT_SCENARIO_DATASET",
                                       "factory_demo_v18.json"),
                "note": "服务端场景时钟；过期窗口须重新生成计划，不得复活旧计划。",
            },
        )
    raise ValueError(
        f"unknown PLANPILOT_CLOCK_MODE={mode!r}; expected 'wall' or 'scenario'")


DEFAULT_CLOCK = WallClock()
