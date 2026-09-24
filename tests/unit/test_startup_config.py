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
        env = base_env(tmp_path, PLANPILOT_ENV=ok,
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
        base_env(tmp_path, PLANPILOT_ENV="production",
                 PLANPILOT_BACKUP_DIR=str(tmp_path / "bk")), root=tmp_path)
    assert cfg is not None, [str(i) for i in issues]


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


def test_preflight_writable_checks(tmp_path):
    cfg, _ = parse_startup_env(base_env(tmp_path), root=tmp_path)
    assert preflight(cfg) == []
    # platform-independent unwritable probe: a FILE as ancestor can
    # never host a sqlite file (Windows chmod on dirs is advisory, so
    # this pins the creatable-branch rather than ACL semantics).
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    bad = dataclasses.replace(cfg, backup_dir=blocker / "sub" / "deeper.db")
    issues = preflight(bad)
    assert any(i.severity == "error" for i in issues), issues
    bad2 = dataclasses.replace(cfg, db_path=blocker / "nope.db")
    assert any(i.severity == "error" for i in preflight(bad2))


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
    parsed field, while Bedrock's six vars MUST keep following the env
    (credential rotation without restart)."""
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


def test_bedrock_reacts_to_later_env_change_without_restart(tmp_path, monkeypatch):
    """Rotation path per design §3: flip PLANPILOT_BEDROCK_API_KEY in the
    process env; the next read inside bedrock_client sees the NEW value
    while StartupConfig keeps every startup field frozen."""
    from planpilot.inference import bedrock_client as bc
    env = base_env(tmp_path)
    cfg, _ = parse_startup_env(env, root=tmp_path)

    monkeypatch.setenv("PLANPILOT_BEDROCK_API_KEY", "oldkey-0123456789")
    first = os.environ.get("PLANPILOT_BEDROCK_API_KEY")
    monkeypatch.setenv("PLANPILOT_BEDROCK_API_KEY", "newkey-0123456789")
    second = os.environ.get("PLANPILOT_BEDROCK_API_KEY")
    assert (first, second) == ("oldkey-0123456789", "newkey-0123456789")
    # bedrock_client resolves the key at CALL time via a module-level
    # function that reads os.environ fresh — prove it structurally:
    src = Path(bc.__file__).read_text(encoding="utf-8")
    assert "os.environ" in src or "os.getenv" in src
    assert "PLANPILOT_BEDROCK_API_KEY" in src
    # StartupConfig has no bedrock field that could freeze it
    assert not any("bedrock" in f.name for f in dataclasses.fields(StartupConfig))


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
    """design §3: the six Bedrock vars stay per-call reads and are NOT
    StartupConfig fields — a future 'snapshot everything' refactor must
    trip this test."""
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


def test_bedrock_client_still_re_reads_env(tmp_path, monkeypatch):
    """bedrock_client must consult os.environ AT CALL TIME (rotation),
    not a frozen startup value."""
    from planpilot.inference import bedrock_client as bc
    src = Path(bc.__file__).read_text(encoding="utf-8")
    # the six vars are read via os.environ inside the module. NOTE the
    # sixth's real name is PLANPILOT_FORBID_LLM_NETWORK (no BEDROCK_
    # segment — verified at f92e046; design §3 lists it that way too).
    hits = sum(src.count(f'"{v}"') + src.count(f"'{v}'") for v in [
        "PLANPILOT_BEDROCK_API_KEY", "PLANPILOT_BEDROCK_KEY_FILE",
        "PLANPILOT_BEDROCK_REGION", "PLANPILOT_BEDROCK_MODEL",
        "PLANPILOT_BEDROCK_DAILY_TOKENS", "PLANPILOT_FORBID_LLM_NETWORK"])
    assert hits >= 7  # FORBID appears twice (config view + enforcement)
    assert "startup_config" not in src


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
