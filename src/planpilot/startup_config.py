"""G4 Phase 2 startup configuration: ONE snapshot, two layers (design §3).

`parse_startup_env(snapshot)` is a PURE function — a dict goes in, a
(parsed config or None, issues) pair comes out. It never touches
`os.environ` and never touches the filesystem; every I/O check lives in
`preflight()`. `api_server.main()` reads `dict(os.environ)` exactly once
and constructs everything from the parsed object — no second read can
race a mid-boot environment change.

Secrets: the FULL value is held on the dataclass (no external SecretStr
type — this repo stays stdlib), marked `repr=False, compare=False` so
`repr()`, `str()` and `__eq__` comparisons cannot echo it, plus an
explicit `summary()` that exposes the non-sensitive config (host, port,
paths, mode) and reports the secret only as present/length.

Intentional exception to the one-shot rule (design §3, claim scoped by
the Batch-A review): the Bedrock CREDENTIAL
(`PLANPILOT_BEDROCK_API_KEY` / `PLANPILOT_BEDROCK_KEY_FILE`, read inside
`_credential()`) and the network switch (`PLANPILOT_FORBID_LLM_NETWORK`)
are PER-CALL reads inside bedrock_client, so credential rotation does
not require a restart. `PLANPILOT_BEDROCK_REGION` / `MODEL` /
`DAILY_TOKENS` are read at BedrockClient CONSTRUCTION — equally not
StartupConfig fields, but no per-call re-read is claimed for them.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path

# The repo ROOT (…/planpilot-build): src/planpilot/startup_config.py ->
# parents[2]. Used ONLY as the development default backup root, exactly
# like backup.py's DB_FILE sibling path — never a fabricated home dir.
ROOT = Path(__file__).resolve().parents[2]

ENVS = ("development", "production")
_SECRET_MIN = 32
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_READY_TIMEOUT_MS = 1500
DEFAULT_CLOCK_MODE = "wall"  # G1.0.1 fail-safe: unset means REAL wall time

# [BATCH-A P1-2, normalised in review-3 #2] tasks 2.4 promised "prod
# localhost => refusal"; production MUST bind non-loopback (Phase 5
# compose sets 0.0.0.0 explicitly). The old exact-tuple match was
# bypassable (`LOCALHOST`, `localhost.`, `::ffff:127.0.0.1`); classify
# structurally instead: ipaddress is_loopback (IPv4-mapped IPv6 too)
# plus a casefolded, trailing-dot-stripped localhost hostname test.
def is_loopback_host(host: str) -> bool:
    h = str(host).strip()
    if h.endswith("]"):            # bracketed IPv6 literal `[::1]`
        h = h[1:-1]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h.rstrip(".").casefold() == "localhost"
    if ip.is_loopback:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


@dataclass(frozen=True)
class Issue:
    """One parse/preflight finding. severity is closed to error|warning."""
    severity: str      # "error" | "warning"
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.field}: {self.message}"


class StartupConfigError(RuntimeError):
    """Internal startup refusal (code+field+message); never a contract code."""
    def __init__(self, code: str, field: str, message: str):
        super().__init__(f"{code} ({field}): {message}")
        self.code, self.field, self.message = code, field, message


@dataclass(frozen=True)
class StartupConfig:
    """Parsed startup fields. Frozen: no attribute is reassigned after boot.

    `auth_secret` holds the full working value but is excluded from
    repr/compare; use .summary() for anything that may be logged.
    """
    env: str
    host: str
    port: int
    db_path: Path
    factory_root: Path
    backup_dir: Path
    clock_mode: str
    ready_timeout_ms: int
    root: Path = ROOT
    auth_secret: str = field(repr=False, compare=False, default="")

    def summary(self) -> dict:
        """Loggable view: non-sensitive config VALUES and paths
        (env/host/port/db/factory_root/backup_dir/clock_mode/timeout)
        plus booleans/counts for the secret — the secret string itself
        never appears [BATCH-A P2: wording made honest; the earlier
        'booleans/counts only' understated what this record carries].
        """
        return {
            "env": self.env,
            "host": self.host,
            "port": self.port,
            "db_path": str(self.db_path),
            "factory_root": str(self.factory_root),
            "backup_dir": str(self.backup_dir),
            "clock_mode": self.clock_mode,
            "ready_timeout_ms": self.ready_timeout_ms,
            "auth_secret_present": bool(self.auth_secret),
            "auth_secret_length": len(self.auth_secret),
        }


def _err(field_: str, message: str) -> Issue:
    return Issue("error", field_, message)


def _warn(field_: str, message: str) -> Issue:
    return Issue("warning", field_, message)


def parse_startup_env(snapshot: dict, *, root: Path | None = None
                      ) -> tuple[StartupConfig | None, list[Issue]]:
    """PURE: dict in, (config|None, issues) out. No os.environ, no I/O.

    Any error-severity issue means config is None — the process must not
    construct Database/Clock/Server and must not bind.
    """
    issues: list[Issue] = []
    root = Path(root) if root is not None else ROOT
    get = snapshot.get  # reads ONLY the passed dict — never os.environ

    env = get("PLANPILOT_ENV", "development")
    if env not in ENVS:
        issues.append(_err("PLANPILOT_ENV",
                           f"must be one of {list(ENVS)}, got {env!r}"))

    host = get("PLANPILOT_HOST", DEFAULT_HOST)
    # [BATCH-A P1-2, reviewer-reproduced] tasks 2.4 promised production
    # non-loopback; the old parse accepted `production + 127.0.0.1`.
    # Review-3 #2: classification is STRUCTURAL now (is_loopback_host) —
    # the exact-tuple set missed `::ffff:127.0.0.1`, `LOCALHOST`,
    # `localhost.`. Phase 5 compose binds 0.0.0.0 explicitly.
    if env == "production" and is_loopback_host(host):
        issues.append(_err("PLANPILOT_HOST",
                           f"production must bind a NON-loopback host "
                           f"(IP is_loopback, IPv4-mapped IPv6, or the "
                           f"localhost hostname in any case/trailing-dot "
                           f"spelling refused), got {host!r}"))
    port_raw = get("PLANPILOT_PORT", str(DEFAULT_PORT))
    try:
        port = int(str(port_raw))
        if not (0 <= port <= 65535):
            raise ValueError
    except ValueError:
        port = -1
        issues.append(_err("PLANPILOT_PORT", f"not a valid TCP port: {port_raw!r}"))

    ready_raw = get("PLANPILOT_READY_TIMEOUT_MS", str(DEFAULT_READY_TIMEOUT_MS))
    try:
        ready_timeout_ms = int(str(ready_raw))
        if ready_timeout_ms < 100:
            raise ValueError
    except ValueError:
        ready_timeout_ms = -1
        issues.append(_err("PLANPILOT_READY_TIMEOUT_MS",
                           f"must be an integer >= 100 ms, got {ready_raw!r}"))

    secret = get("PLANPILOT_AUTH_SECRET", "") or ""
    if len(secret) < _SECRET_MIN:
        # Same rule in EVERY environment (design §3: dev boots only WITH
        # a valid secret; the Server-level check stays as the belt).
        issues.append(_err("PLANPILOT_AUTH_SECRET",
                           "missing or shorter than 32 characters"))

    db_raw = get("PLANPILOT_DB", "planpilot.db")
    db_path = Path(db_raw)
    if env == "production" and not db_path.is_absolute():
        issues.append(_err("PLANPILOT_DB",
                           f"production must use an absolute db path, got {db_raw!r}"))

    # Backup root policy (1.0.1 round-5 #4): development defaults to the
    # repo ROOT/backups; production has NO implicit default — it must say
    # where (compose will set /backups). Never Path(home), never $PATH.
    backup_raw = get("PLANPILOT_BACKUP_DIR")
    if backup_raw:
        backup_dir = Path(backup_raw)
    elif env == "production":
        backup_dir = None
        issues.append(_err("PLANPILOT_BACKUP_DIR",
                           "production requires an explicit backup directory"))
    else:
        backup_dir = root / "backups"

    clock_mode = get("PLANPILOT_CLOCK_MODE", DEFAULT_CLOCK_MODE)

    factory_root = Path(get("PLANPILOT_FACTORY_ROOT", str(root / "data")))

    if issues:
        return None, issues

    warnings: list[Issue] = []
    if snapshot.get("PLANPILOT_HOST") is None:
        warnings.append(_warn("PLANPILOT_HOST", f"default_used {DEFAULT_HOST}"))
    if snapshot.get("PLANPILOT_PORT") is None:
        warnings.append(_warn("PLANPILOT_PORT", f"default_used {DEFAULT_PORT}"))
    if snapshot.get("PLANPILOT_READY_TIMEOUT_MS") is None:
        warnings.append(_warn("PLANPILOT_READY_TIMEOUT_MS",
                              f"default_used {DEFAULT_READY_TIMEOUT_MS}"))
    if snapshot.get("PLANPILOT_CLOCK_MODE") is None:
        warnings.append(_warn("PLANPILOT_CLOCK_MODE",
                              f"default_used {DEFAULT_CLOCK_MODE} (G1.0.1 fail-safe)"))

    cfg = StartupConfig(
        env=env, host=host, port=port, db_path=db_path,
        factory_root=factory_root, backup_dir=backup_dir,
        clock_mode=clock_mode, ready_timeout_ms=ready_timeout_ms,
        root=root, auth_secret=secret)

    # clock policy validation is pure over the snapshot + parsed host
    # (G1 §2.1b): scenario on a public bind refuses BEFORE construction.
    from planpilot.clock import clock_from_env
    try:
        clock_from_env(dict(snapshot), host)
    except ValueError as exc:
        return None, issues + [_err("PLANPILOT_CLOCK_MODE", str(exc))]
    return cfg, warnings


def preflight(cfg: StartupConfig) -> list[Issue]:
    """ALL filesystem/network sanity in one place (the I/O layer)."""
    issues: list[Issue] = []
    if not cfg.factory_root.is_dir():
        issues.append(_err("PLANPILOT_FACTORY_ROOT",
                           f"must be an EXISTING directory: {cfg.factory_root}"))
    # PLANPILOT_DB is a WRITE target: it (or its parent) must be creatable
    # and writable, or Database construction fails with a raw sqlite error
    # instead of an operator-readable refusal.
    _check_write_location(cfg.db_path, "PLANPILOT_DB", issues)
    # [G4 5.3, design §6] PLANPILOT_BACKUP_DIR is NOT a server write target.
    # The server only READS `last_verified_backup.json` from it (deep-health
    # backup age, P1-4 — never an mtime guess); the WRITER is the separate
    # scheduled-backup service, which mounts the same volume rw and does not
    # go through this preflight. Requiring writability here contradicted the
    # mandated production mount `backups:/backups:ro` and made the container
    # unbootable.
    #   production: the dir is a real mount point — it must EXIST and be
    #     readable (a missing/unreadable backup dir would make deep health
    #     silently report no manifest forever, a production gate failure
    #     nobody can diagnose);
    #   development: the default ROOT/backups may not exist yet — an absent
    #     dev backup dir is benign (no verified backups yet); when it DOES
    #     exist it must be readable.
    if cfg.backup_dir is not None:
        bd = Path(cfg.backup_dir)
        if bd.exists() and not bd.is_dir():
            issues.append(_err("PLANPILOT_BACKUP_DIR",
                               f"exists but is not a directory: {bd}"))
        elif cfg.env == "production":
            if not bd.is_dir():
                issues.append(_err("PLANPILOT_BACKUP_DIR",
                                   f"production requires an EXISTING directory "
                                   f"(the server reads the verified-backup "
                                   f"manifest from it): {bd}"))
            elif not _readable(bd):
                issues.append(_err("PLANPILOT_BACKUP_DIR",
                                   f"directory not readable: {bd}"))
        elif bd.is_dir() and not _readable(bd):
            issues.append(_err("PLANPILOT_BACKUP_DIR",
                               f"directory not readable: {bd}"))
    issues.extend(_port_checks(cfg))
    return issues


def _check_write_location(path, label: str, issues: list[Issue]) -> None:
    """A WRITE target's location: writable if present, else creatable."""
    p = Path(path)
    if p.exists():
        if p.is_dir():
            if not _writable(p):
                issues.append(_err(label, f"directory not writable: {p}"))
        else:
            parent = p.parent
            if not (parent.is_dir() and _writable(parent)):
                issues.append(_err(label, f"file location not writable: {parent}"))
    else:
        probe = p.parent if not p.is_dir() else p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not (probe.is_dir() and _writable(probe)):
            issues.append(_err(label, f"not creatable/writable under {probe}"))


def _readable(d: Path) -> bool:
    """Can we LIST this directory? (The manifest read itself is checked by
    deep health at request time; a directory we cannot even list can never
    yield one.)"""
    try:
        next(iter(d.iterdir()), None)
    except OSError:
        return False
    return True


def _writable(d: Path) -> bool:
    try:
        fd, probe = _touch_probe(d)
    except OSError:
        return False
    try:
        return True
    finally:
        try:
            import os
            os.close(fd)
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def _touch_probe(d: Path):
    import os
    import tempfile
    fd, name = tempfile.mkstemp(prefix=".preflight-", dir=str(d))
    return fd, Path(name)


def _port_checks(cfg: StartupConfig) -> list[Issue]:
    """BIND+RELEASE test (no connect — 'can I own this port', not 'is
    something there'). The main() path reaches this only BEFORE Server
    construction, so a bound port here is a genuine conflict."""
    issues = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((cfg.host, cfg.port))
    except OSError as exc:
        issues.append(_err("PLANPILOT_HOST/PLANPILOT_PORT",
                           f"cannot bind {cfg.host}:{cfg.port} — {exc}"))
    return issues


def config_provenance(cfg: StartupConfig) -> dict:
    """G4 2.3: the loggable, REDACTED record of what the boot snapshot
    resolved to. It carries the NON-SENSITIVE config (host, port, the
    three paths, env, clock mode, timeout) plus secret PRESENCE and
    LENGTH only — the secret value never appears (length is metadata,
    not the secret) [BATCH-A P2: honest scope, was 'booleans/counts'].
    Provenance means "one snapshot, read at boot, shown honestly"; the
    source field states that contract in the record itself."""
    return {"source": "startup_snapshot_once", **cfg.summary()}


def startup_or_die(snapshot: dict, *, root: Path | None = None
                   ) -> tuple[StartupConfig, list[Issue]]:
    """Convenience for main(): parse + preflight, raising
    StartupConfigError (the process refuses) on ANY error issue."""
    cfg, issues = parse_startup_env(snapshot, root=root)
    if cfg is not None:
        pre = preflight(cfg)
        issues = issues + pre
        if any(i.severity == "error" for i in pre):
            cfg = None
    errors = [i for i in issues if i.severity == "error"]
    if cfg is None or errors:
        first = errors[0] if errors else Issue("error", "startup", "parse refused")
        raise StartupConfigError("startup_config_refused", first.field,
                                 "; ".join(str(i) for i in errors) or str(first))
    return cfg, [i for i in issues if i.severity == "warning"]
