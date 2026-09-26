"""G4 5.2 — deploy gate: pure env logic + honest BLOCKED semantics."""
import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "deploy_gate", ROOT / "tools" / "deploy_gate.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclass string-annotation resolution needs the module registered
    # BEFORE exec_module (reviewer pitfall class: importlib half-load)
    sys.modules["deploy_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


G = load_gate()
GOOD_SECRET = "x" * 40


def env_for(profile="development", secret=GOOD_SECRET, host=None, **extra):
    e = {"PLANPILOT_ENV": profile, "PLANPILOT_AUTH_SECRET": secret}
    if host is not None:
        e["PLANPILOT_HOST"] = host
    e.update(extra)
    return e


# ---------- layer 1: pure logic ----------
def test_profile_mismatch_is_fail_both_directions():
    f = {x.check: x for x in G.evaluate_env(env_for("production"), "development")}
    assert f["profile_matches_env"].status == G.FAIL
    f = {x.check: x for x in G.evaluate_env(env_for("development"), "production")}
    assert f["profile_matches_env"].status == G.FAIL


def test_production_profile_without_env_is_fail():
    # startup defaults PLANPILOT_ENV to development — a production
    # deployment claimed with the env UNSET would silently boot dev.
    e = {"PLANPILOT_AUTH_SECRET": GOOD_SECRET}
    f = {x.check: x for x in G.evaluate_env(e, "production")}
    assert f["profile_matches_env"].status == G.FAIL


def test_development_profile_without_env_defaults_agree():
    e = {"PLANPILOT_AUTH_SECRET": GOOD_SECRET}
    f = {x.check: x for x in G.evaluate_env(e, "development")}
    assert f["profile_matches_env"].status == G.PASS


def test_weak_secret_fails_in_every_profile():
    for prof in ("development", "production"):
        e = env_for(prof, secret="t00-short")
        f = {x.check: x for x in G.evaluate_env(e, prof)}
        assert f["auth_secret"].status == G.FAIL
        # detail must NOT echo the secret itself
        assert "t00-short" not in f["auth_secret"].detail


def test_production_refuses_loopback_accepts_real_bind():
    bad = G.evaluate_env(env_for("production", host="127.0.0.1"), "production")
    assert {x.check: x.status for x in bad}["bind_host"] == G.FAIL
    mapped = G.evaluate_env(env_for("production", host="::ffff:127.0.0.1"),
                            "production")
    assert {x.check: x.status for x in mapped}["bind_host"] == G.FAIL
    good = G.evaluate_env(env_for("production", host="0.0.0.0"), "production")
    assert {x.check: x.status for x in good}["bind_host"] == G.PASS


def test_unknown_profile_short_circuits():
    f = G.evaluate_env(env_for("development"), "staging")
    assert len(f) == 1 and f[0].status == G.FAIL and f[0].check == "profile_valid"


# ---------- layer 2: credential channel logic ----------
def test_key_file_channel_api_key_pass():
    f = G.probe_key_file({"PLANPILOT_BEDROCK_API_KEY": "AKIAfakefakefake"})
    assert f.status == G.PASS
    assert "AKIAfakefakefake" not in f.detail  # value never echoed


def test_key_file_channel_both_set_is_fail():
    f = G.probe_key_file({"PLANPILOT_BEDROCK_API_KEY": "k" * 30,
                          "PLANPILOT_BEDROCK_KEY_FILE": "/tmp/pp.key"})
    assert f.status == G.FAIL


def test_key_file_channel_neither_set_is_BLOCKED_not_pass():
    f = G.probe_key_file({})
    assert f.status == G.BLOCKED  # "no runnable target" rule


# --- 5.2.2 (reviewer P1): the ENV channel must face the SAME token
# rules the runtime applies; env semantics = NO stripping (the client
# takes os.environ.get() verbatim), unlike the file channel.
@pytest.mark.parametrize("value,why", [
    ("line1\nline2", "not single-line"),
    ("pass\xc3\xa9-word", "non-ASCII"),
    ("A" * 20000, "longer than 16384"),
    ("  AKIAdashdashdash  ", "not single-line"),   # outer spaces NOT tolerated on env
    ("AKIA with spaces inside", "not single-line"),
])
def test_env_key_violations_are_fail_not_pass(value, why):
    f = G.probe_key_file({"PLANPILOT_BEDROCK_API_KEY": value})
    assert f.status == G.FAIL, f.detail
    assert why in f.detail
    stripped = value.strip()
    if stripped:
        assert stripped[:10] not in f.detail  # value never echoed


def test_env_key_clean_single_line_ascii_still_passes():
    f = G.probe_key_file({"PLANPILOT_BEDROCK_API_KEY": "AKIA" + "f" * 30})
    assert f.status == G.PASS, f.detail
    assert "single-line ASCII" in f.detail


def test_key_file_inside_repo_is_fail(tmp_path):
    inside = ROOT / "src" / "sneaky.key"
    try:
        inside.write_text("dummy")
        f = G.probe_key_file({"PLANPILOT_BEDROCK_KEY_FILE": str(inside)})
        assert f.status == G.FAIL
        assert "inside the repo" in f.detail
    finally:
        inside.unlink(missing_ok=True)


def test_key_file_outside_repo_passes_with_platform_note(tmp_path):
    kf = tmp_path / "pp.key"
    kf.write_text("dummy")  # tmp_path is outside ROOT by construction
    f = G.probe_key_file({"PLANPILOT_BEDROCK_KEY_FILE": str(kf)})
    assert f.status == G.PASS
    if sys.platform == "win32":
        assert "platform note" in f.detail  # limitation must be RECORDED


# ---------- layer 3 + exit mapping ----------
def test_tracked_secrets_probe_green_on_this_repo():
    # Layer-2 scan must be CLEAN on a frozen tree. Fixture files that
    # intentionally construct adversarial content
    # (negative_control_workspace.py mutates docs with a fake AWS key;
    # validate_kiro_workspace.py checks the EMPTY .env placeholder)
    # are excluded BY NAME — if either is renamed the probe re-hits
    # them and this test surfaces the drift.
    f = G.probe_tracked_secrets()
    if f.status == G.BLOCKED:
        pytest.skip("git ls-files unavailable (e.g. no-.git negctl "
                    "sandbox) — BLOCKED is the honest outcome there")
    assert f.status == G.PASS, f.detail


def test_docker_absent_is_BLOCKED_with_G4_warning(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    f = G.probe_docker()
    assert f.status == G.BLOCKED
    assert "G4" in f.detail  # the finding itself warns: don't close G4


def test_http_unreachable_target_is_BLOCKED():
    # 127.0.0.1:9 is discard — connect refused, i.e. NO live deployment
    f = G.probe_http_ready("http://127.0.0.1:9")
    assert f.status == G.BLOCKED


def test_exit_code_priority_fail_beats_blocked():
    r = G.GateReport(profile="production")
    r.findings = [G.Finding("a", G.PASS, "", 1), G.Finding("b", G.BLOCKED, "", 3)]
    assert (r.worst, r.exit_code) == (G.BLOCKED, 2)
    r.findings.append(G.Finding("c", G.FAIL, "", 1))
    assert (r.worst, r.exit_code) == (G.FAIL, 1)
    ok = G.GateReport(profile="development",
                      findings=[G.Finding("a", G.PASS, "", 1)])
    assert (ok.worst, ok.exit_code) == (G.PASS, 0)


def test_docker_absent_caps_whole_run_at_exit_two(monkeypatch):
    # The "no runnable target => BLOCKED, never PASS" rule wired to the
    # process contract: with ONLY layer 3 on a docker-less host, the
    # run's worst status is blocked and its exit code is 2.
    monkeypatch.setattr("shutil.which", lambda name: None)
    report = G.run_gate("development", {}, include_layers=(3,))
    assert report.worst == G.BLOCKED
    assert report.exit_code == 2
    assert {f.check: f.status for f in report.findings} == {"docker": G.BLOCKED}


# ---------- 5.2.1 reviewer acceptance matrix ----------
class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_docker_live_daemon_broken_compose_is_fail(monkeypatch):
    # Reviewer #1: daemon reachable but `compose config` rejects the
    # file must be FAIL — the old probe returned PASS on `docker info`
    # alone. [G4 5.3] The gate now selects BOTH profiles and injects a
    # throwaway placeholder secret so compose's required-interpolation
    # (`:?`) cannot be mis-reported as a broken file.
    monkeypatch.setattr("shutil.which", lambda name: "docker")
    seen = {}

    def fake_run(cmd, **kw):
        if cmd[1] == "info":
            return _R(0, "27.1.1\n")
        if cmd[1] == "compose" and cmd[-2:] == ["config", "--quiet"]:
            # pin the profile selection AND the placeholder-secret env
            seen["cmd"] = cmd
            seen["env_secret"] = (kw.get("env") or {}).get(
                "PLANPILOT_AUTH_SECRET")
            return _R(1, "", "yaml: line 3: mapping values are not allowed")
        raise AssertionError(f"unexpected docker subcommand: {cmd}")
    monkeypatch.setattr(G.subprocess, "run", fake_run)
    f = G.probe_docker()
    assert f.status == G.FAIL
    assert "compose.yaml REJECTED" in f.detail
    # both profile-gated services are validated, never a vacuous config
    assert "--profile" in seen["cmd"] and "production" in seen["cmd"] \
        and "operations" in seen["cmd"]
    # the real secret is NEVER touched: a constant placeholder only, and
    # it must not be mistaken for a credential leak
    assert seen["env_secret"] == G._COMPOSE_PLACEHOLDER_SECRET


def test_docker_live_daemon_valid_compose_is_pass(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "docker")

    def fake_run(cmd, **kw):
        return _R(0, "27.1.1\n") if cmd[1] == "info" else _R(0, "")
    monkeypatch.setattr(G.subprocess, "run", fake_run)
    f = G.probe_docker()
    assert f.status == G.PASS
    assert "compose config valid" in f.detail


def test_http_live_503_target_is_fail_not_blocked():
    # Reviewer #2 with a REAL server: HTTPError subclasses URLError,
    # so the old code reported a live-but-unready 503 as BLOCKED.
    import http.server
    import threading

    class FiftyThree(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"db":"error"}')

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), FiftyThree)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        f = G.probe_http_ready(f"http://127.0.0.1:{srv.server_address[1]}")
        assert f.status == G.FAIL, f.detail
        assert "HTTP 503" in f.detail
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_200_db_ok_is_pass():
    import http.server
    import threading

    class Green(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"db":"ok"}')

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Green)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        f = G.probe_http_ready(f"http://127.0.0.1:{srv.server_address[1]}")
        assert f.status == G.PASS, f.detail
    finally:
        srv.shutdown()
        srv.server_close()


# connection refused stays BLOCKED (already covered by
# test_http_unreachable_target_is_BLOCKED above).

def test_key_path_directory_is_fail(tmp_path):
    # Reviewer #3: a directory used to PASS the exists() check.
    d = tmp_path / "fakekeydir"
    d.mkdir()
    f = G.probe_key_file({"PLANPILOT_BEDROCK_KEY_FILE": str(d)})
    assert f.status == G.FAIL
    assert "not a regular file" in f.detail


@pytest.mark.parametrize("content,why", [
    ("", "empty"),
    ("   \n", "empty"),
    ("line1\nline2\n", "single-line"),
    ("key\u00e9key", "non-ASCII"),
    ("A" * 20000, "16384"),
])
def test_key_content_violations_are_fail(tmp_path, content, why):
    kf = tmp_path / "pp.key"
    kf.write_text(content, encoding="utf-8")
    f = G.probe_key_file({"PLANPILOT_BEDROCK_KEY_FILE": str(kf)})
    assert f.status == G.FAIL, f.detail
    assert why in f.detail
    stripped = content.strip()
    if stripped:
        # first line of the (invalid) key must never appear in detail
        assert stripped.splitlines()[0][:10] not in f.detail


def test_key_content_valid_passes(tmp_path):
    kf = tmp_path / "pp.key"
    kf.write_text("sk-valid-single-line-ascii-key", encoding="utf-8")
    f = G.probe_key_file({"PLANPILOT_BEDROCK_KEY_FILE": str(kf)})
    assert f.status == G.PASS, f.detail


# ---------- 5.2.1 wheelhouse download guard (reviewer P2) ----------
def test_download_refuses_dir_with_existing_wheels(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "build_wheelhouse", ROOT / "tools" / "build_wheelhouse.py")
    bw = importlib.util.module_from_spec(spec)
    sys.modules["build_wheelhouse"] = bw
    spec.loader.exec_module(bw)
    (tmp_path / "unrelated_stale-1.0-py3-none-any.whl").write_bytes(b"PK\x03\x04junk")
    rc = bw.cmd_download(argparse.Namespace(wheel_dir=tmp_path, python=sys.executable))
    assert rc == 3
    # and it refused BEFORE touching pip: the stale wheel is untouched
    assert (tmp_path / "unrelated_stale-1.0-py3-none-any.whl").exists()
