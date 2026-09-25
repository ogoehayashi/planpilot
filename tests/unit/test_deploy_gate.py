"""G4 5.2 — deploy gate: pure env logic + honest BLOCKED semantics."""
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
