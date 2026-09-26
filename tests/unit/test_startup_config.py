"""G4 Phase 2 — startup_config: one snapshot, two layers (design §3).

parse_startup_env is PURE (dict in / no I/O); preflight owns the I/O.
Every assertion here either calls the pure function directly or checks
that no filesystem/env access happened at all.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from planpilot import startup_config as sc
from planpilot.startup_config import (
    Issue, StartupConfig, StartupConfigError, parse_startup_env, preflight,
    startup_or_die)

GOOD_SECRET = "s" * 40


def base_env(tmp: Path, **over) -> dict:
    env = {
        "PLANPILOT_ENV": "development",
        "PLANPILOT_AUTH_SECRET": GOOD_SECRET,
        "PLANPILOT_DB": str(tmp / "planpilot.db"),
        "PLANPILOT_FACTORY_ROOT": str(tmp),
    }
    env.update(over)
    return env


# ---------------------------------------------------------------- pure ----

def test_parse_is_pure_no_os_environ_and_no_fs(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANPILOT_AUTH_SECRET", "from-the-real-env-0123456789ab")
    monkeypatch.setenv("PLANPILOT_ENV", "production")
    # A dict with NOTHING: pure parse sees only the snapshot passed in.
    cfg, issues = parse_startup_env({}, root=tmp_path)
    assert cfg is None
    fields = {i.field for i in issues}
    # the real env's secret must NOT satisfy the empty snapshot
    assert "PLANPILOT_AUTH_SECRET" in fields
    # no file was created anywhere by parsing: tmp_path still empty
    assert list(tmp_path.iterdir()) == []


def test_defaults_single_source(tmp_path):
    cfg, warnings = parse_startup_env(base_env(tmp_path), root=tmp_path)
    assert cfg is not None, [str(i) for i in warnings]
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8080
    assert cfg.ready_timeout_ms == 1500
    assert cfg.clock_mode == "wall"
    assert cfg.backup_dir == tmp_path / "backups"       # dev default ROOT/backups
    assert {i.field for i in warnings} >= {"PLANPILOT_HOST", "PLANPILOT_PORT",
                                           "PLANPILOT_READY_TIMEOUT_MS"}
    assert all(i.severity == "warning" for i in warnings)


def test_unknown_env_refused(tmp_path):
    cfg, issues = parse_startup_env(base_env(tmp_path, PLANPILOT_ENV="staging"),
                                    root=tmp_path)
    assert cfg is None
    assert any(i.field == "PLANPILOT_ENV" for i in issues)


def test_env_closed_values_only_accepts_two(tmp_path):
    for ok in ("development", "production"):
        # [BATCH-A P1-2] production cannot keep the loopback DEFAULT host,
        # so the prod leg pins an explicit non-loopback bind (0.0.0.0 as
        # Phase 5 compose will); this test is about the ENV VALUE seam.
        env = base_env(tmp_path, PLANPILOT_ENV=ok, PLANPILOT_HOST="0.0.0.0",
                       PLANPILOT_DB=str(tmp_path / "db.sqlite"),
                       PLANPILOT_BACKUP_DIR=str(tmp_path / "bk"))
        cfg, issues = parse_startup_env(env, root=tmp_path)
        assert cfg is not None, [str(i) for i in issues]


def test_secret_min_32_both_envs(tmp_path):
    for mode in ("development", "production"):
        cfg, issues = parse_startup_env(
            base_env(tmp_path, PLANPILOT_ENV=mode, PLANPILOT_AUTH_SECRET="x" * 31,
                     PLANPILOT_BACKUP_DIR=str(tmp_path / "bk")), root=tmp_path)
        assert cfg is None, mode
        assert any("32" in i.message and i.field == "PLANPILOT_AUTH_SECRET"
                   for i in issues)
    cfg, issues = parse_startup_env(
        base_env(tmp_path, PLANPILOT_ENV="production", PLANPILOT_HOST="0.0.0.0",
                 PLANPILOT_BACKUP_DIR=str(tmp_path / "bk")), root=tmp_path)
    assert cfg is not None, [str(i) for i in issues]


# [BATCH-A P1-2] production must REFUSE loopback binds (tasks 2.4
# promised it; the reviewer reproduced that the old parse accepted it).
# Phase 5 compose binds 0.0.0.0 explicitly, so implement the refusal.
# [review-3 #2] the classification is STRUCTURAL (ipaddress.is_loopback,
# ipv4_mapped, casefolded trailing-dot-stripped localhost) — the old
# exact-tuple set let `LOCALHOST` / `localhost.` / `::ffff:127.0.0.1`
# through, which are pinned HERE as refusals.
@pytest.mark.parametrize("bad_host", [
    "127.0.0.1", "localhost", "::1",
    "LOCALHOST", "LocalHost", "localhost.", "localhost..",
    "::ffff:127.0.0.1", "[::1]", "127.5.5.5",  # whole 127/8 is loopback
])
def test_production_refuses_loopback(tmp_path, bad_host):
    cfg, issues = parse_startup_env(
        base_env(tmp_path, PLANPILOT_ENV="production", PLANPILOT_HOST=bad_host,
                 PLANPILOT_BACKUP_DIR=str(tmp_path / "bk")), root=tmp_path)
    assert cfg is None, bad_host
    assert any(i.field == "PLANPILOT_HOST" and "loopback" in i.message.lower()
               for i in issues), [str(i) for i in issues]


def test_production_accepts_nonloopback_and_development_keeps_loopback(tmp_path):
    cfg, issues = parse_startup_env(
        base_env(tmp_path, PLANPILOT_ENV="production", PLANPILOT_HOST="0.0.0.0",
                 PLANPILOT_BACKUP_DIR=str(tmp_path / "bk")), root=tmp_path)
    assert cfg is not None and cfg.host == "0.0.0.0", [str(i) for i in issues]
    # development keeps the loopback default untouched (dev UX unchanged)
    dev, _ = parse_startup_env(base_env(tmp_path), root=tmp_path)
    assert dev is not None and dev.host == "127.0.0.1"


def test_production_requires_explicit_backup_dir(tmp_path):
    env = base_env(tmp_path, PLANPILOT_ENV="production",
                   PLANPILOT_DB=str(tmp_path / "p.db"))
    env.pop("PLANPILOT_BACKUP_DIR", None)
    cfg, issues = parse_startup_env(env, root=tmp_path)
    assert cfg is None
    assert any(i.field == "PLANPILOT_BACKUP_DIR" for i in issues)


def test_production_requires_absolute_db(tmp_path):
    cfg, issues = parse_startup_env(
        base_env(tmp_path, PLANPILOT_ENV="production", PLANPILOT_DB="rel.db",
                 PLANPILOT_BACKUP_DIR=str(tmp_path / "bk")), root=tmp_path)
    assert cfg is None
    assert any(i.field == "PLANPILOT_DB" for i in issues)


def test_bad_port_and_timeout_refused(tmp_path):
    for k, v in (("PLANPILOT_PORT", "notaport"),
                 ("PLANPILOT_READY_TIMEOUT_MS", "12ms"),
                 ("PLANPILOT_READY_TIMEOUT_MS", "50")):
        cfg, issues = parse_startup_env(base_env(tmp_path, **{k: v}),
                                        root=tmp_path)
        assert cfg is None, (k, v)
        assert any(i.field == k for i in issues)


def test_scenario_clock_on_public_host_refused_by_parse(tmp_path):
    cfg, issues = parse_startup_env(
        base_env(tmp_path, PLANPILOT_HOST="0.0.0.0",
                 PLANPILOT_CLOCK_MODE="scenario"), root=tmp_path)
    assert cfg is None
    assert any(i.field == "PLANPILOT_CLOCK_MODE" for i in issues)


# ------------------------------------------------------ secret hygiene ----

def test_secret_frozen_and_hidden_everywhere(tmp_path):
    cfg, _ = parse_startup_env(base_env(tmp_path), root=tmp_path)
    assert isinstance(cfg, StartupConfig)
    assert cfg.auth_secret == GOOD_SECRET          # full working value kept
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.auth_secret = "mutated"
    assert GOOD_SECRET not in repr(cfg)
    assert GOOD_SECRET not in str(cfg)
    assert GOOD_SECRET not in StartupConfig.__doc__
    assert GOOD_SECRET not in str(cfg.summary())
    # compare=False: two configs identical except the secret still compare
    other = dataclasses.replace(cfg, auth_secret="d" * 40)
    assert cfg == other


def test_error_carries_field_not_value(tmp_path):
    cfg, issues = parse_startup_env(
        base_env(tmp_path, PLANPILOT_AUTH_SECRET="short"), root=tmp_path)
    joined = " ".join(str(i) for i in issues)
    assert "short" not in joined.replace("shorter", "")   # no secret echo


# ---------------------------------------------------------- preflight ----

def test_preflight_factory_root_must_be_existing_dir(tmp_path):
    cfg, _ = parse_startup_env(base_env(tmp_path, PLANPILOT_PORT="0"),
                               root=tmp_path)
    assert preflight(cfg) == []          # port 0 = ephemeral bind, never flaky
    missing = dataclasses.replace(cfg, factory_root=tmp_path / "nope")
    issues = preflight(missing)
    assert any(i.severity == "error" and "EXISTING directory" in i.message
               for i in issues)
    # a FILE (not a directory) also refuses
    f = tmp_path / "afile"
    f.write_text("x")
    issues = preflight(dataclasses.replace(cfg, factory_root=f))
    assert issues and all(i.severity == "error" for i in issues)


def test_preflight_db_write_location_is_creatable(tmp_path):
    cfg, _ = parse_startup_env(base_env(tmp_path), root=tmp_path)
    assert preflight(cfg) == []
    # platform-independent unwritable probe: a FILE as ancestor can
    # never host a sqlite file (Windows chmod on dirs is advisory, so
    # this pins the creatable-branch rather than ACL semantics). PLANPILOT_DB
    # IS a server write target, so an impossible location is an error.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    bad2 = dataclasses.replace(cfg, db_path=blocker / "nope.db")
    assert any(i.severity == "error" and i.field == "PLANPILOT_DB"
               for i in preflight(bad2))


def test_preflight_backup_dir_is_read_not_write_target(tmp_path):
    """[G4 5.3, design §6] The server only READS the verified-backup
    manifest from PLANPILOT_BACKUP_DIR; the mandated production mount is
    `backups:/backups:ro`. So preflight must NOT demand writability —
    it demands an existing READABLE dir in production (a real mount
    point), tolerates an absent dev dir, and refuses a non-directory in
    every env. Requiring writability here was the exact bug that made
    the read-only production container unbootable."""
    # development: an absent backup dir is benign (no verified backups yet)
    dev, _ = parse_startup_env(base_env(tmp_path, PLANPILOT_PORT="0"),
                               root=tmp_path)
    dev_absent = dataclasses.replace(dev, backup_dir=tmp_path / "not-yet")
    assert preflight(dev_absent) == []
    # development: an EXISTING readable dir is fine
    good = tmp_path / "bk-good"
    good.mkdir()
    assert preflight(dataclasses.replace(dev, backup_dir=good)) == []
    # a FILE at the backup dir is refused in every env (not a directory)
    f = tmp_path / "bk-is-a-file"
    f.write_text("x")
    for env, extra in (("development", {}),
                       # production also refuses the loopback default host
                       ("production", {"PLANPILOT_HOST": "0.0.0.0"})):
        c, issues = parse_startup_env(base_env(
            tmp_path, PLANPILOT_ENV=env, PLANPILOT_PORT="0",
            PLANPILOT_BACKUP_DIR=str(f), PLANPILOT_DB=str(tmp_path / "p.db"),
            **extra), root=tmp_path)
        assert c is not None, (env, [str(i) for i in issues])
        found = preflight(c)
        assert any(i.field == "PLANPILOT_BACKUP_DIR" and i.severity == "error"
                   for i in found), (env, found)
    # production: an ABSENT backup dir is refused (the mount must exist)
    prod_absent = tmp_path / "prod-mount-missing"
    pc, _ = parse_startup_env(base_env(
        tmp_path, PLANPILOT_ENV="production", PLANPILOT_HOST="0.0.0.0",
        PLANPILOT_PORT="0", PLANPILOT_BACKUP_DIR=str(prod_absent),
        PLANPILOT_DB=str(tmp_path / "p.db")), root=tmp_path)
    assert pc is not None
    assert any(i.field == "PLANPILOT_BACKUP_DIR"
               and "production requires an EXISTING directory" in i.message
               for i in preflight(pc))
    # production: a read-only (non-writable) dir is ACCEPTED — the whole point
    ro_dir = tmp_path / "prod-ro"
    ro_dir.mkdir()
    rpc, _ = parse_startup_env(base_env(
        tmp_path, PLANPILOT_ENV="production", PLANPILOT_HOST="0.0.0.0",
        PLANPILOT_PORT="0", PLANPILOT_BACKUP_DIR=str(ro_dir),
        PLANPILOT_DB=str(tmp_path / "p.db")), root=tmp_path)
    assert rpc is not None
    assert preflight(rpc) == []           # readable dir => OK, no write demand


def test_startup_or_die_raises_without_construction(tmp_path, monkeypatch):
    env = base_env(tmp_path, PLANPILOT_AUTH_SECRET="nope")
    with pytest.raises(StartupConfigError) as ei:
        startup_or_die(env, root=tmp_path)
    assert "PLANPILOT_AUTH_SECRET" in str(ei.value)
    assert ei.value.code == "startup_config_refused"
    # and the happy path returns cfg + warning issues only
    cfg, warnings = startup_or_die(base_env(tmp_path, PLANPILOT_PORT="0"),
                                   root=tmp_path)
    assert cfg.port == 0 and all(w.severity == "warning" for w in warnings)


# --------------------------------------------- G4 2.7 startup regression --

def test_main_snapshots_os_environ_exactly_once(monkeypatch, tmp_path):
    """main() converts os.environ to a dict ONCE and refuses (SystemExit,
    zero construction) when the snapshot is invalid."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import api_server

    reads = []
    sentinel = "__pp_marker_" + "z" * 40
    store = {"PLANPILOT_AUTH_SECRET": sentinel,
             "PLANPILOT_ENV": "staging",       # unconditionally invalid
             "PLANPILOT_FACTORY_ROOT": str(tmp_path),
             "PLANPILOT_DB": str(tmp_path / "refused.db"),
             "PLANPILOT_PORT": "0"}

    class EnvSpy:  # NOT a dict subclass — dict() then must use keys()+[ ]
        def keys(self):
            reads.append("*keys*")
            return store.keys()

        def __getitem__(self, k):
            reads.append(k)
            return store[k]

        def __setitem__(self, k, v):
            store[k] = v                    # fixtures may write during test

        def __delitem__(self, k):
            store.pop(k, None)

        def get(self, k, default=None):
            return store.get(k, default)

        def __contains__(self, k):
            return k in store

    monkeypatch.setattr(api_server.os, "environ", EnvSpy())
    with pytest.raises(SystemExit) as ei:
        api_server.main()
    assert "startup config refused" in str(ei.value)
    assert reads.count("*keys*") == 1, "main() must snapshot os.environ exactly once"
    assert "PLANPILOT_ENV" in reads             # fields read FROM the snapshot
    assert not (tmp_path / "refused.db").exists()  # zero construction


def test_startup_fields_never_follow_later_env_changes(tmp_path):
    """G4 2.7 (design §3): startup config is fixed by the ONE snapshot —
    later os.environ changes (bad or benign) never move an already
    parsed field, while Bedrock's CREDENTIAL and NETWORK SWITCH keep
    following the env (rotation without restart; claim scoped by the
    Batch-A review — region/model/daily limit belong to
    BedrockClient.__init__, not StartupConfig, and are NOT re-read per
    call)."""
    env = base_env(tmp_path)
    cfg, _ = parse_startup_env(env, root=tmp_path)
    # simulate a hostile/benign reconfiguration AFTER startup:
    mutated = dict(env)
    mutated.update({"PLANPILOT_AUTH_SECRET": "h" * 40, "PLANPILOT_PORT": "9999",
                    "PLANPILOT_HOST": "0.0.0.0", "PLANPILOT_DB": "evil.db"})
    cfg2, _ = parse_startup_env(mutated, root=tmp_path)
    # a re-parse of a DIFFERENT snapshot is allowed (fresh process);
    # the point is the LIVE object never re-reads:
    assert cfg.host == "127.0.0.1" and cfg.port == 8080
    assert cfg.db_path == Path(str(tmp_path / "planpilot.db"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.port = 9999
    # and mutating the dict that was passed in cannot retro-change it
    env["PLANPILOT_PORT"] = "1234"
    assert cfg.port == 8080


def test_bedrock_credential_rotation_is_a_real_per_call_reread(tmp_path, monkeypatch):
    """[BATCH-A P1-4] The reviewer's demand: prove rotation with a REAL
    BedrockClient against a LIVE environment — construct once, flip the
    env var, call _credential() again, see the NEW key. The old test
    only asserted os.environ appeared somewhere in the module source,
    which would still pass if the value were cached at import."""
    from planpilot.persistence import Database
    from planpilot.inference.bedrock_client import BedrockClient

    db = Database(tmp_path / "pp.db")
    try:
        monkeypatch.setenv("PLANPILOT_BEDROCK_API_KEY", "oldkey-0123456789")
        client = BedrockClient(db)          # constructed on the OLD key
        assert client._credential() == "oldkey-0123456789"
        monkeypatch.setenv("PLANPILOT_BEDROCK_API_KEY", "newkey-0123456789")
        # no restart, no re-construction: the SAME live object resolves
        # the rotated credential at call time
        assert client._credential() == "newkey-0123456789"
        # StartupConfig keeps every startup field frozen while this works
        cfg, _ = parse_startup_env(base_env(tmp_path), root=tmp_path)
        assert cfg.host == "127.0.0.1"
        assert not any("bedrock" in f.name
                       for f in dataclasses.fields(StartupConfig))
    finally:
        db.close()


def test_server_still_enforces_secret_after_parse(tmp_path):
    """belt & braces (design §3): Server.__init__'s >=32 check stays
    byte-for-byte even though parse already enforces it."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import api_server
    with pytest.raises(ValueError) as ei:
        api_server.Server.__init__.__doc__  # exists
        class _DB:  # not constructed past the secret check
            pass
        api_server.Server(("127.0.0.1", 0), _DB(), "x" * 31, tmp_path)
    assert "32" in str(ei.value)



def test_bedrock_vars_not_frozen_into_config(tmp_path):
    """design §3 (claim scoped by the Batch-A review): the Bedrock vars
    are NOT StartupConfig fields — a future 'snapshot everything'
    refactor must trip this test. Whether each var is re-read per call
    or at client construction is pinned BEHAVIOURALLY below, never by
    grepping the source."""
    frozen_vars = ["PLANPILOT_BEDROCK_API_KEY", "PLANPILOT_BEDROCK_KEY_FILE",
                   "PLANPILOT_BEDROCK_REGION", "PLANPILOT_BEDROCK_MODEL",
                   "PLANPILOT_BEDROCK_DAILY_TOKENS",
                   "PLANPILOT_FORBID_LLM_NETWORK"]
    field_names = {f.name for f in dataclasses.fields(StartupConfig)}
    for v in frozen_vars:
        stem = v.lower().replace("planpilot_", "").replace("planpilot_bedrock_", "")
        assert stem not in field_names
        assert not any(stem in fn for fn in field_names), v
    # snapshot containing them parses identically to one without
    env = base_env(tmp_path)
    cfg1, _ = parse_startup_env(env, root=tmp_path)
    cfg2, _ = parse_startup_env({**env, "PLANPILOT_BEDROCK_MODEL": "other"}, root=tmp_path)
    assert cfg1 == cfg2


def test_bedrock_runtime_reads_are_scoped_behaviourally(tmp_path, monkeypatch):
    """[BATCH-A P1-4] The reviewer: the old test counted the six var
    NAMES in the module SOURCE — text, not behaviour. Pin what the
    code actually does, by CALLING it:
      * credential (API key): per-call re-read — proven by the
        rotation test above;
      * PLANPILOT_FORBID_LLM_NETWORK: per-call (status/converse consult
        os.environ at call time, so flipping it takes effect live);
      * region / model / daily limit: construction-time ONLY — a flip
        after __init__ does NOT move them on the live client, and we no
        longer CLAIM it does. They are not StartupConfig fields either."""
    from planpilot.persistence import Database
    from planpilot.inference.bedrock_client import BedrockClient

    monkeypatch.setenv("PLANPILOT_BEDROCK_API_KEY", "key-0123456789")
    monkeypatch.delenv("PLANPILOT_FORBID_LLM_NETWORK", raising=False)
    db = Database(tmp_path / "pp.db")
    try:
        client = BedrockClient(db)
        # network switch: per-call — flipping it changes status() on the
        # SAME live client with no restart
        assert client.status()["network_enabled"] is False
        monkeypatch.setenv("PLANPILOT_FORBID_LLM_NETWORK", "0")
        assert client.status()["network_enabled"] is True
        # converse() obeys the SAME live switch (blocked network = error,
        # never a silent pass)
        monkeypatch.setenv("PLANPILOT_FORBID_LLM_NETWORK", "1")
        import pytest as _pytest
        from planpilot.inference.bedrock_client import InferenceError
        with _pytest.raises(InferenceError):
            client.converse("s", "p", run_id="run_x1", actor="t")
        # construction-time fields: a later env flip must NOT move them
        monkeypatch.setenv("PLANPILOT_BEDROCK_DAILY_TOKENS", "424242")
        monkeypatch.setenv("PLANPILOT_BEDROCK_REGION", "eu-west-1")
        assert client.daily_limit != 424242
        assert client.region != "eu-west-1"
        # ...while a FRESH construction does see them (proves the read is
        # construction-time, not import-time-cached)
        fresh = BedrockClient(db)
        assert fresh.daily_limit == 424242 and fresh.region == "eu-west-1"
    finally:
        db.close()


def test_main_with_valid_env_assembles_and_closes_cleanly(tmp_path, monkeypatch, capsys):
    """[BATCH-A P0] The reviewer: every main() test so far used an INVALID
    config that exits before construction, so the legal path — where
    cfg.backup_dir must meet Server's backup_root parameter — crashed
    with AttributeError in review. This test runs main() on a LEGAL
    snapshot with a fake serve_forever that closes the listening socket,
    and proves: (1) the Database really is constructed, (2) the Server
    received cfg.backup_dir as its backup_root, (3) after
    serve_forever returns BOTH server and db are closed, (4) zero
    AttributeError."""
    import json as _json
    import socket
    import threading
    from urllib.request import urlopen

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import api_server

    db_path = tmp_path / "planpilot.db"
    backup_dir = tmp_path / "bk"
    legal = {
        "PLANPILOT_ENV": "development",
        "PLANPILOT_AUTH_SECRET": GOOD_SECRET,
        "PLANPILOT_DB": str(db_path),
        "PLANPILOT_FACTORY_ROOT": str(tmp_path),
        "PLANPILOT_BACKUP_DIR": str(backup_dir),
        "PLANPILOT_PORT": "0",            # ephemeral: no race on the bind
    }
    # os.environ itself (not just os.environ.get) must return the legal
    # snapshot — main() snapshots dict(os.environ), so monkeypatch the
    # whole mapping with a real dict copy carrying ONLY these keys.
    monkeypatch.setattr(api_server.os, "environ", dict(legal))

    seen = {}

    class ServerSpy(api_server.Server):
        def serve_forever(self):
            # capture live wiring BEFORE main()'s finally touches
            # anything, then serve EXACTLY ONE real request (the
            # watchdog's /health/live) and return — main()'s finally then
            # runs the real shutdown (server_close + db.close), which is
            # the wiring the reviewer demands we prove.
            seen["db_obj"] = self.db
            seen["db_path"] = str(self.db.path)
            seen["backup_root"] = str(self.backup_root)
            seen["addr"] = self.server_address
            seen["ready_timeout_ms"] = self.ready_timeout_ms
            self.handle_request()

    monkeypatch.setattr(api_server, "Server", ServerSpy)

    def watchdog():
        # once main() reaches our fake serve_forever, poke /health/live
        # for real; if we somehow never get there, force one dead
        # connection so the test FAILS visibly instead of hanging
        import time as _t
        deadline = _t.monotonic() + 30
        while _t.monotonic() < deadline:
            addr = seen.get("addr")
            if addr and addr[1]:
                try:
                    with urlopen(f"http://127.0.0.1:{addr[1]}/health/live",
                                 timeout=5) as r:
                        seen["live"] = (r.status, _json.loads(r.read()))
                except OSError as e:
                    seen["live_error"] = str(e)
                    try:
                        s = socket.create_connection(
                            ("127.0.0.1", addr[1]), timeout=2)
                        s.close()
                    except OSError:
                        pass
                return
            _t.sleep(0.02)

    t = threading.Thread(target=watchdog, daemon=True)
    t.start()
    api_server.main()                     # returns after fake serve_forever
    t.join(timeout=10)

    assert "live_error" not in seen, seen.get("live_error")
    # (1) Database really constructed at the snapshot path
    assert seen["db_path"] == str(db_path) and db_path.exists()
    # (2) THE P0 ASSERTION: Server.backup_root == cfg.backup_dir value
    assert seen["backup_root"] == str(backup_dir)
    assert seen["ready_timeout_ms"] == 1500
    # (3) /health/live answered while it was up
    assert seen["live"][0] == 200
    # (4) both closed after serve_forever: main()'s finally must have
    # called server_close + db.close. [review-3 #1] The DB proof is the
    # REVIEWER's shape: keep the live Database object and show its
    # connection refuses use — any SQL raises sqlite3.ProgrammingError.
    import sqlite3
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", seen["addr"][1]), timeout=1)
    with pytest.raises(sqlite3.ProgrammingError):
        seen["db_obj"].conn.execute("SELECT 1")


def test_main_closes_database_even_if_server_construction_fails(tmp_path, monkeypatch):
    """[review-3 #1] Both constructors must live INSIDE the try: when
    Server(...) itself raises, the already-open Database must still be
    closed. Old code constructed outside the try/finally and leaked."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import api_server

    db_path = tmp_path / "planpilot.db"
    legal = {
        "PLANPILOT_ENV": "development",
        "PLANPILOT_AUTH_SECRET": GOOD_SECRET,
        "PLANPILOT_DB": str(db_path),
        "PLANPILOT_FACTORY_ROOT": str(tmp_path),
        "PLANPILOT_BACKUP_DIR": str(tmp_path / "bk"),
        "PLANPILOT_PORT": "0",
    }
    monkeypatch.setattr(api_server.os, "environ", dict(legal))

    captured = {}

    class ExplodingServer(api_server.Server):
        def __init__(self, *a, **k):
            captured["db"] = a[1]          # the positional db arg
            raise OSError("simulated bind failure after Database()")

    monkeypatch.setattr(api_server, "Server", ExplodingServer)
    with pytest.raises(OSError, match="simulated bind failure"):
        api_server.main()

    # the leaked-handle bug dies here: the DB opened before the failure
    # must be closed anyway (reviewer's exact proof shape)
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        captured["db"].conn.execute("SELECT 1")


def test_config_provenance_is_redacted(tmp_path):
    """G4 2.3: the boot record states provenance once and NEVER carries
    the secret value — length is metadata, the string itself is not."""
    from planpilot.startup_config import config_provenance
    env = base_env(tmp_path)
    cfg, _ = parse_startup_env(env, root=tmp_path)
    record = config_provenance(cfg)
    assert record["source"] == "startup_snapshot_once"
    assert record["auth_secret_present"] is True
    assert record["auth_secret_length"] == len(env["PLANPILOT_AUTH_SECRET"])
    # the raw secret must appear NOWHERE in the serialised record
    import json as _json
    assert env["PLANPILOT_AUTH_SECRET"] not in _json.dumps(record)
    assert env["PLANPILOT_AUTH_SECRET"] not in repr(record)
    # and main() logs exactly this function, once, before construction
    root = Path(__file__).resolve().parents[2]
    src = (root / "tools" / "api_server.py").read_text(encoding="utf-8")
    main_body = src[src.index("def main():"):]
    assert main_body.count("config_provenance(") == 1
    assert main_body.index("config_provenance(") < main_body.index("Database(")
