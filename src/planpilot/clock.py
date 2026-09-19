"""Server-owned clock for every authoritative timestamp (G1, 2026-09-19).

The contract makes expiry server-owned (security_controls.expiry_is_server_owned)
and its clamping rule is evaluated against ``server_now``. That means the
timestamp source is a security boundary: a caller who can supply their own
"now" can walk an expired plan back into life and dodge APPROVAL_EXPIRED.

So no request path may carry a timestamp. Every server-side timestamp flows
through the Clock object injected into RuntimeAuthority:

- ``WallClock``  — the default. The machine's real time pinned to
  Asia/Singapore (+08:00), matching persistence.now().
- ``FixedClock`` — deterministic time for unit tests and replay harnesses.
  Injected by the server operator, never by a client.
- ``ScenarioClock`` — a WallClock that reports the scenario it pins the demo
  dataset to. The UI must surface it so a scenario-dated demonstration is
  never mistaken for production-dated output (P1-3 web approval rule).

Switching the clock never rewrites stored plan data: digests are immutable
and computed over content only, so demo/replay/fixed timestamps cannot change
an installed plan.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))


class Clock:
    """Abstract server-owned clock."""

    kind = "wall"
    scenario: dict | None = None

    def now(self) -> str:
        raise NotImplementedError

    def status(self) -> dict:
        return {"kind": self.kind, "now": self.now(), "scenario": self.scenario}


class WallClock(Clock):
    """Real machine time, rendered in Asia/Singapore like every stored stamp."""

    kind = "wall"

    def now(self) -> str:
        return datetime.now(SGT).isoformat()


class FixedClock(Clock):
    """Deterministic clock for tests and replay; the operator injects it."""

    kind = "fixed"

    def __init__(self, iso: str, scenario: dict | None = None):
        # Re-serialise via isoformat() so the stamp always carries the 'T'
        # separator the contract date-time format demands (str(datetime)
        # would emit a space separator).
        self._now = datetime.fromisoformat(iso).isoformat(timespec="seconds")
        self.scenario = scenario

    def advance(self, seconds: float) -> None:
        self._now = (
            datetime.fromisoformat(self._now) + timedelta(seconds=seconds)
        ).isoformat()

    def now(self) -> str:
        return self._now


class ScenarioClock(FixedClock):
    """A FixedClock that declares the scenario it pins. The UI shows this."""

    kind = "scenario"


DEFAULT_CLOCK = WallClock()
