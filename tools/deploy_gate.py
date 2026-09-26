"""Deploy gate (G4 Phase 5.2, design §6): profile-checked deploy truth.

LAYER 1  evaluate_env(env, profile) -> findings  — PURE dict logic, no
         filesystem or network: env validity, secret strength,
         production bind policy (delegated to startup_config's
         structural loopback check), profile<->PLANPILOT_ENV mismatch.
LAYER 2  probe_filesystem(...)  — I/O: tracked-file secret scan (git
         ls-files), Bedrock key-file placement (API_KEY env OR managed
         key FILE — contract allows the emailed key from env; both are
         legitimate), wheelhouse manifest integrity via
         build_wheelhouse.verify.
LAYER 3  probe_targets(...)  — I/O against EXTERNAL targets: the docker
         gate (5.2.1) is daemon reachability (`docker info`) PLUS
         `docker compose config` on the tracked compose.yaml — a live
         daemon whose compose file is broken is FAIL, not PASS; docker
         absent => BLOCKED. HTTP /health/ready (--gate=http) classifies
         by what actually happened: connection/DNS/timeout => BLOCKED
         (no live target); any HTTP response (incl. 503) => FAIL unless
         200 with db=="ok" => PASS. A check with NO runnable target
         returns status BLOCKED, NEVER PASS — and BLOCKED caps the
         run's exit at 2. Docker gate not green => container acceptance
         unresolved => exit 2 => G4 stays honestly BLOCKED (tasks 5.3).

Exit codes: 0 = every executed check PASS (no BLOCKED), 1 = at least
one FAIL, 2 = no FAIL but one or more BLOCKED checks. FAIL beats
BLOCKED beats PASS. No bypass switch exists (any flag that would skip
a probe does not exist), while the default run has no way to pretend a
blocked check passed.

Machine-readable: --json emits one findings object on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from planpilot.startup_config import (  # noqa: E402
    ENVS,
    _SECRET_MIN,
    is_loopback_host,
)

PASS, FAIL, BLOCKED = "pass", "fail", "blocked"

# [G4 5.3] Constant throwaway value used ONLY to satisfy compose.yaml's
# required-secret interpolation (`PLANPILOT_AUTH_SECRET:?`) while the docker
# gate validates FILE validity. It is not a credential, is never read from the
# environment, is never logged, and never reaches a container — the real
# secret's presence/strength is owned by layer-1 `auth_secret`.
_COMPOSE_PLACEHOLDER_SECRET = "deploy-gate-compose-syntax-probe-placeholder"


@dataclass
class Finding:
    check: str
    status: str
    detail: str
    layer: int

    def as_dict(self) -> dict:
        return {"check": self.check, "status": self.status,
                "detail": self.detail, "layer": self.layer}


@dataclass
class GateReport:
    profile: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def worst(self) -> str:
        statuses = {f.status for f in self.findings}
        if FAIL in statuses:
            return FAIL
        if BLOCKED in statuses:
            return BLOCKED
        return PASS

    @property
    def exit_code(self) -> int:
        return {PASS: 0, FAIL: 1, BLOCKED: 2}[self.worst]

    def as_dict(self) -> dict:
        return {"profile": self.profile, "overall": self.worst,
                "exit_code": self.exit_code,
                "findings": [f.as_dict() for f in self.findings]}


def evaluate_env(env: dict, profile: str) -> list[Finding]:
    """LAYER 1 — pure. `env` is a plain mapping (os.environ-shaped)."""
    out: list[Finding] = []
    real = env.get("PLANPILOT_ENV", "")
    if profile not in ENVS:
        out.append(Finding("profile_valid", FAIL,
                           f"unknown --profile {profile!r} (allowed: {', '.join(ENVS)})", 1))
        return out
    out.append(Finding("profile_valid", PASS, profile, 1))
    # design §6: --profile production must AGREE with PLANPILOT_ENV.
    # startup parse DEFAULTS unset env to development, so: production
    # profile + unset = the runtime would NOT be production (mismatch);
    # development profile + unset = defaults agree (pass).
    if real and real != profile:
        out.append(Finding("profile_matches_env", FAIL,
                           f"--profile={profile} but PLANPILOT_ENV={real!r} (mismatch)", 1))
    elif not real and profile == "production":
        out.append(Finding("profile_matches_env", FAIL,
                           "--profile=production but PLANPILOT_ENV unset — startup would default to development", 1))
    else:
        out.append(Finding("profile_matches_env", PASS, real or "development (default)", 1))

    secret = env.get("PLANPILOT_AUTH_SECRET", "") or ""
    if len(secret) < _SECRET_MIN:
        out.append(Finding("auth_secret", FAIL,
                           f"PLANPILOT_AUTH_SECRET missing or shorter than {_SECRET_MIN} chars (length {len(secret)})", 1))
    else:
        out.append(Finding("auth_secret", PASS,
                           f"present, length {len(secret)} (value not shown)", 1))

    host = env.get("PLANPILOT_HOST", "127.0.0.1")
    if profile == "production":
        if is_loopback_host(host):
            out.append(Finding("bind_host", FAIL,
                               f"production refuses loopback bind {host!r}", 1))
        else:
            out.append(Finding("bind_host", PASS, str(host), 1))
    else:
        out.append(Finding("bind_host", PASS,
                           f"development binds {host!r}", 1))
    return out
# --- LAYER 2: filesystem probes -----------------------------------------
import re  # noqa: E402
import stat as statmod  # noqa: E402

# Secret-pattern scan over TRACKED text files (design §6 layer 2). We
# match assignment-shaped literals only — a bare word like "token" in
# prose must not scream; an actual `SECRET = "..."`/bearer header does.
_SECRET_RE = re.compile(
    r"""(?ix)
    (?:(?:api[_-]?key|auth[_-]?secret|secret[_-]?key|password|passwd|
        access[_-]?token|bearer[_-]?token)\s*[:=]\s*["'][^"']{8,}["'])
    |(?:AKIA[0-9A-Z]{16})
    |(?:sk-[A-Za-z0-9]{20,})
    """
)
# Test fixtures deliberately seed fake secrets; the scan covers shipped
# surfaces (src/, tools/, compose.yaml, Dockerfile), never tests/.
_SCAN_TARGETS = ("src", "tools", "compose.yaml", "Dockerfile",
                 "requirements.txt", "requirements-import.txt")


def _tracked_files() -> list[Path] | None:
    try:
        res = subprocess.run(
            ["git", "ls-files", "--", *[str(t) for t in _SCAN_TARGETS]],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return [ROOT / line for line in res.stdout.splitlines() if line.strip()]


def probe_tracked_secrets() -> Finding:
    files = _tracked_files()
    if files is None:
        return Finding("tracked_secrets", BLOCKED,
                       "git ls-files unavailable — cannot enumerate tracked files", 2)
    hits: list[str] = []
    # Test fixtures that CONSTRUCT adversarial content (mutating a doc
    # to contain fake AWS keys) and placeholder lines with EMPTY values
    # are the scan's known false-positive class — exclude by explicit,
    # reviewed marker, never by loosening the regex itself.
    scan_targets = [p for p in files
                    if "negative_control" not in p.name
                    and "validate_kiro" not in p.name]
    for path in scan_targets:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _SECRET_RE.finditer(text):
            rel = path.relative_to(ROOT).as_posix()
            line = text[:m.start()].count("\n") + 1
            hits.append(f"{rel}:{line}")
    if hits:
        return Finding("tracked_secrets", FAIL,
                       "secret-shaped literals in tracked files: " + ", ".join(hits[:8]), 2)
    return Finding("tracked_secrets", PASS,
                   f"{len(scan_targets)} tracked files scanned, 0 matches", 2)


def _validate_key_value(token, strip_outer=False):
    """Shared Bedrock-credential token rules, mirrored EXACTLY from
    BedrockClient._credential() (bedrock_client.py ~line 110): a
    token survives iff it is a str, non-empty, <=16384 chars, pure
    ASCII, and free of whitespace anywhere. strip_outer=True applies
    the FILE channel's .strip() semantics (the runtime strips file
    content after reading); the ENV channel must NOT strip — the
    runtime takes os.environ.get() verbatim, so a leading/trailing
    space fails there too. Returns None when valid, else a violation
    description that NEVER contains the value (5.2.2 reviewer #P1)."""
    if not isinstance(token, str):
        return "not a string"
    if strip_outer:
        token = token.strip()
    if not token:
        return "empty or whitespace-only"
    if len(token) > 16384:
        return "longer than 16384 characters"
    if not token.isascii():
        return "contains non-ASCII characters"
    if any(c.isspace() for c in token):
        return "not single-line (whitespace inside the key)"
    return None


def _validate_key_content(kf: Path):
    """File channel read semantics, identical to the runtime: utf-8-sig,
    capped read of 16385, outer whitespace stripped, then the shared
    token rules."""
    try:
        with kf.open("r", encoding="utf-8-sig") as stream:
            token = stream.read(16385)
    except (OSError, UnicodeError) as exc:
        return f"unreadable as text ({type(exc).__name__})"
    return _validate_key_value(token, strip_outer=True)


def probe_key_file(env: dict) -> Finding:
    """Bedrock credential: env var OR managed key file (both legal)."""
    api_key = env.get("PLANPILOT_BEDROCK_API_KEY", "") or ""
    kf_raw = env.get("PLANPILOT_BEDROCK_KEY_FILE", "") or ""
    if api_key and kf_raw:
        return Finding("bedrock_credential", FAIL,
                       "PLANPILOT_BEDROCK_API_KEY env AND PLANPILOT_BEDROCK_KEY_FILE both set — pick one channel", 2)
    if api_key:
        # 5.2.2 (reviewer P1): the env channel used to PASS on ANY
        # non-empty value, while BedrockClient._credential() rejects
        # multiline / non-ASCII / over-16384 / whitespace-containing
        # tokens outright — the gate said "credential ok" and the
        # agent then failed at call time. Run the env token through the
        # SAME rules with the runtime's env semantics (no stripping).
        bad = _validate_key_value(api_key, strip_outer=False)
        if bad:
            return Finding("bedrock_credential", FAIL,
                           f"PLANPILOT_BEDROCK_API_KEY present but invalid — {bad}; "
                           "BedrockClient would refuse it at call time (value not shown)", 2)
        return Finding("bedrock_credential", PASS,
                       f"PLANPILOT_BEDROCK_API_KEY env present, valid single-line ASCII key "
                       f"(length {len(api_key)}, value not shown)", 2)
    if not kf_raw:
        return Finding("bedrock_credential", BLOCKED,
                       "no PLANPILOT_BEDROCK_API_KEY env and no PLANPILOT_BEDROCK_KEY_FILE — inference probes cannot run (never reported as pass)", 2)
    kf = Path(kf_raw)
    if not kf.is_absolute():
        return Finding("bedrock_credential", FAIL,
                       f"PLANPILOT_BEDROCK_KEY_FILE {kf_raw!r} is not an absolute path", 2)
    try:
        rp = kf.resolve()
    except OSError:
        return Finding("bedrock_credential", FAIL, f"PLANPILOT_BEDROCK_KEY_FILE {kf_raw!r} unresolvable", 2)
    if ROOT in rp.parents or rp == ROOT:
        return Finding("bedrock_credential", FAIL,
                       "PLANPILOT_BEDROCK_KEY_FILE lives inside the repo — the managed key must be OUTSIDE any checkout", 2)
    if kf.is_symlink():
        return Finding("bedrock_credential", FAIL,
                       "PLANPILOT_BEDROCK_KEY_FILE is a symlink — refuse (redir/swap risk)", 2)
    # 5.2.1 (reviewer #3): exists() let a DIRECTORY masquerade as the
    # key file. Require a regular file, then enforce the SAME content
    # rules BedrockClient._credential() applies at use-time — a gate
    # PASS must never promise a file the client would then reject.
    if not kf.is_file():
        return Finding("bedrock_credential", FAIL,
                       f"PLANPILOT_BEDROCK_KEY_FILE {kf_raw!r} does not exist or is not a regular file", 2)
    problem = _validate_key_content(kf)
    if problem:
        return Finding("bedrock_credential", FAIL,
                       f"PLANPILOT_BEDROCK_KEY_FILE fails BedrockClient rules: {problem} (content not echoed)", 2)
    if os.name == "posix":
        mode = statmod.S_IMODE(kf.stat().st_mode)
        if mode & 0o077:
            return Finding("bedrock_credential", FAIL,
                           f"PLANPILOT_BEDROCK_KEY_FILE mode {mode:04o} is group/other-readable (need <=0600)", 2)
        return Finding("bedrock_credential", PASS,
                       f"managed key file outside repo, mode {mode:04o}", 2)
    # Windows has no POSIX modes — record the substitution honestly
    # (design §6: the limitation itself is part of the finding).
    return Finding("bedrock_credential", PASS,
                   "managed key file outside repo, not a symlink"
                   " [platform note: POSIX mode<=0600 not enforceable on Windows NTFS ACLs here]", 2)


def probe_wheelhouse(env: dict) -> Finding:
    """Manifest + real wheels must verify or the container build cannot pin deps."""
    import contextlib
    import io
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import build_wheelhouse as bw  # noqa: WPS433
    except ImportError as exc:
        return Finding("wheelhouse", FAIL, f"build_wheelhouse import failed: {exc}", 2)
    wheel_dir = Path(env.get("PLANPILOT_WHEELHOUSE_DIR", str(bw.DEFAULT_WHEEL_DIR)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bw.cmd_verify(argparse.Namespace(wheel_dir=wheel_dir))
    if rc == 0:
        return Finding("wheelhouse", PASS,
                       f"manifest verified: {buf.getvalue().strip()}", 2)
    return Finding("wheelhouse", FAIL,
                   f"wheelhouse verify exited {rc} for {wheel_dir}: {buf.getvalue().strip()}", 2)


def probe_git_state() -> Finding:
    try:
        res = subprocess.run(["git", "status", "--porcelain"],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return Finding("worktree_clean", BLOCKED, "git unavailable", 2)
    if res.returncode != 0:
        return Finding("worktree_clean", BLOCKED, "git status failed", 2)
    dirty = [l for l in res.stdout.splitlines() if l.strip()]
    if dirty:
        return Finding("worktree_clean", FAIL,
                       f"{len(dirty)} uncommitted paths — freeze the tree before deploying", 2)
    return Finding("worktree_clean", PASS, "working tree clean", 2)
# --- LAYER 3: external targets (absent target => BLOCKED, never PASS) --
COMPOSE_FILE = ROOT / "compose.yaml"


def probe_docker() -> Finding:
    """Docker gate = daemon reachability AND compose-config validity.

    Reviewer 5.2.1 #1: `docker info` alone PASSed while a broken
    compose.yaml would fail the actual `docker compose up`. The gate
    checks in order — binary, daemon, then `docker compose config` on
    the tracked compose.yaml — and the first failing stage decides the
    status. Absent binary/daemon => BLOCKED (nothing was actually
    verified); daemon live but compose config broken => FAIL (a real
    defect, not a missing target).

    [G4 5.3] Two isolations, both empirically pinned on this host:
    (a) `config` skips profile-gated services unless the profile is
        selected — so we select BOTH `production` (api) and `operations`
        (backup), otherwise the check is vacuous (`services: {}`).
    (b) compose.yaml uses REQUIRED interpolation (`PLANPILOT_AUTH_SECRET:?`)
        so a host with no secret set would make `config` fail — but that
        is a SECRET-PRESENCE defect owned by layer-1 `auth_secret`, NOT a
        broken compose file. Mis-reporting it as "compose.yaml REJECTED"
        would be a wrong-reason FAIL. We therefore validate FILE validity
        with a constant throwaway placeholder secret injected into the
        subprocess env only — the REAL secret is never read, never passed,
        never logged here. `config` validates schema + interpolation
        shape; it does NOT resolve build contexts (a missing out-of-repo
        wheelhouse is caught by the live build, not this syntax gate).
    """
    from shutil import which
    if which("docker") is None:
        return Finding("docker", BLOCKED,
                       "docker binary absent on this host — container acceptance UNRESOLVED (tasks 5.3); G4 must not close on this run", 3)
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return Finding("docker", BLOCKED, "docker present but `docker info` failed/timed out", 3)
    if info.returncode != 0:
        return Finding("docker", BLOCKED,
                       f"docker present but daemon not answering: {info.stderr.strip()[:120]}", 3)
    # daemon live — the design's second sub-check: compose config
    if not COMPOSE_FILE.exists():
        return Finding("docker", FAIL,
                       "daemon live but tracked compose.yaml is missing on disk", 3)
    # Isolate FILE validity from secret presence: validate with both
    # profiles selected and a constant throwaway secret (never the real
    # one, never logged).
    cfg_env = dict(os.environ, PLANPILOT_AUTH_SECRET=_COMPOSE_PLACEHOLDER_SECRET)
    cfg_env.pop("PYTHONUTF8", None)
    try:
        cfg = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE),
             "--profile", "production", "--profile", "operations",
             "config", "--quiet"],
            capture_output=True, text=True, timeout=60, env=cfg_env)
    except (OSError, subprocess.TimeoutExpired):
        return Finding("docker", FAIL,
                       f"daemon live (server {info.stdout.strip()}) but `docker compose config` timed out/failed to launch", 3)
    if cfg.returncode != 0:
        return Finding("docker", FAIL,
                       "daemon live but compose.yaml REJECTED: " + cfg.stderr.strip()[:200], 3)
    return Finding("docker", PASS,
                   f"daemon reachable (server {info.stdout.strip()}) AND compose config valid (both profiles, placeholder secret)", 3)


def probe_http_ready(base_url: str) -> Finding:
    """Classify by what ACTUALLY happened (reviewer 5.2.1 #2).

    urllib.HTTPError subclasses URLError, so a blanket
    `except (URLError, ...)` turned a live-but-unready 503 into
    BLOCKED — the opposite of the truth (a target IS deployed).
    Rule: never reached the server (connection refused / DNS /
    timeout) => BLOCKED; an HTTP response arrived => the target is
    live, and any status other than 200-with-db-ok is FAIL.
    """
    import urllib.request
    import urllib.error
    url = base_url.rstrip("/") + "/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return Finding("http_ready", FAIL,
                       f"live target at {url} answered HTTP {exc.code} — /health/ready NOT green", 3)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return Finding("http_ready", BLOCKED,
                       f"no live /health/ready at {url}: {type(exc).__name__}", 3)
    except json.JSONDecodeError as exc:
        return Finding("http_ready", FAIL, f"/health/ready not JSON: {exc}", 3)
    if resp.status == 200 and body.get("db") == "ok":
        return Finding("http_ready", PASS, url, 3)
    return Finding("http_ready", FAIL,
                   f"status {resp.status}, db={body.get('db')!r}", 3)


def run_gate(profile: str, env: dict, *, want_http: str | None = None,
             include_layers: tuple[int, ...] = (1, 2, 3)) -> GateReport:
    report = GateReport(profile=profile)
    if 1 in include_layers:
        report.findings += evaluate_env(env, profile)
    if 2 in include_layers:
        report.findings += [probe_tracked_secrets(), probe_key_file(env),
                            probe_wheelhouse(env), probe_git_state()]
    if 3 in include_layers:
        report.findings.append(probe_docker())
        if want_http:
            report.findings.append(probe_http_ready(want_http))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", required=True, choices=list(ENVS))
    ap.add_argument("--gate", default="all",
                    choices=["all", "env", "fs", "docker", "http"],
                    help="which layers to run; default all (1+2+3)")
    ap.add_argument("--base-url", default=os.environ.get("PLANPILOT_BASE_URL", ""),
                    help="with --gate=http: live base URL to probe /health/ready")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    layers = {"all": (1, 2, 3), "env": (1,), "fs": (1, 2),
              "docker": (1, 2, 3), "http": (1, 2, 3)}[args.gate]
    want_http = args.base_url if args.gate == "http" else None
    if args.gate == "http" and not args.base_url:
        # an http gate with no target must say so, not silently shrink
        report = run_gate(args.profile, dict(os.environ),
                          include_layers=layers)
        report.findings.append(Finding("http_ready", FAIL,
                                       "--gate=http without --base-url: no live target given", 3))
    else:
        report = run_gate(args.profile, dict(os.environ), want_http=want_http,
                          include_layers=layers)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        for f in report.findings:
            print(f"[{f.status.upper():<7}] L{f.layer} {f.check}: {f.detail}")
        print(f"overall={report.worst} exit={report.exit_code}")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())

