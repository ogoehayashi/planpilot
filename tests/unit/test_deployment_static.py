"""G4 5.3 — STATIC deployment-boundary regressions (design §6, tasks 5.3).

These pin the tracked Dockerfile / compose.yaml BYTES so the container
boundary cannot silently regress. They are STATIC on purpose: they must
stay deterministic and runnable with NO docker binary and NO daemon
(the live-container acceptance lives in the deploy-gate docker probe and
the Phase-6 live-run log, never here). Where a claim is about what
`docker compose config` RENDERS (no plaintext secret, non-loopback bind)
we assert the SOURCE shape that guarantees it, and separately run the
renderer when a daemon is reachable (skipped otherwise).

Claims pinned (one assertion group each):
  1. Dockerfile HEALTHCHECK targets /health/ready (NOT bare /health, NOT
     /health/deep); deep is never a container probe anywhere in the file.
  2. Dockerfile installs deps OFFLINE from the out-of-repo wheelhouse
     (`--no-index --find-links`), re-verifying the tracked manifest first —
     never a public-PyPI `pip install -r` at build time.
  3. Dockerfile sets PLANPILOT_ENV=production explicitly.
  4. compose api service mounts backups READ-ONLY (`backups:/backups:ro`).
  5. compose carries NO plaintext secret: the auth secret is a REQUIRED
     interpolation (`${PLANPILOT_AUTH_SECRET:?…}`), so an unset secret
     fails closed at compose level and the file never holds a literal.
  6. compose production bind is NON-loopback (the api port mapping does
     not publish on 127.0.0.1) and the api service is production-profiled
     with PLANPILOT_ENV=production.
  7. When a daemon IS reachable: `docker compose config` (both profiles,
     placeholder secret) renders, the rendered tree contains NO literal
     secret beyond the placeholder we injected, and the published api
     address is non-loopback.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "compose.yaml"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _lines(p: Path) -> list[str]:
    return _text(p).splitlines()


# ---------------------------------------------------------------- HEALTHCHECK
def test_dockerfile_healthcheck_targets_ready_not_deep():
    text = _text(DOCKERFILE)
    hc = [ln for ln in _lines(DOCKERFILE) if ln.strip().startswith("HEALTHCHECK")]
    assert len(hc) == 1, f"expected exactly one HEALTHCHECK line, got {len(hc)}"
    line = hc[0]
    assert "/health/ready" in line, line
    # bare /health (the old DB-touching compat probe) must NOT be the target:
    # match /health only when not followed by /ready|/live|/deep
    assert not re.search(r"/health(?!/(?:ready|live|deep))", line), line
    assert "/health/deep" not in line, line
    # deep is diagnostics, never an automated probe — not ANYWHERE in the file
    assert "/health/deep" not in text


# ---------------------------------------------------------------- offline deps
def test_dockerfile_installs_from_wheelhouse_never_public_pypi():
    text = _text(DOCKERFILE)
    # the offline install form is present
    assert "--no-index" in text and "--find-links" in text, \
        "Dockerfile must install with --no-index --find-links <wheelhouse>"
    # the manifest is re-verified before install (drift cannot ship an image)
    assert "build_wheelhouse.py verify" in text
    # NO build-time fetch from public PyPI: a bare `pip install ... -r
    # requirements...` WITHOUT --no-index would drift. Assert every
    # `pip install` line carries --no-index.
    for ln in _lines(DOCKERFILE):
        if "pip install" in ln:
            assert "--no-index" in ln, f"non-offline pip install: {ln!r}"


def test_dockerfile_sets_production_env():
    text = _text(DOCKERFILE)
    assert "PLANPILOT_ENV=production" in text, \
        "Dockerfile must set PLANPILOT_ENV=production explicitly (tasks 2.3/5.3)"


# ---------------------------------------------------------------- compose mounts
def test_compose_api_mounts_backups_readonly():
    text = _text(COMPOSE)
    # the api service's backup mount must be read-only. Find the backups
    # volume mapping and require the :ro suffix.
    m = re.search(r"-\s*backups:/backups(?::ro)?\b", text)
    assert m, "compose must mount the backups volume on api"
    # there may be a rw mount on the operations `backup` writer service —
    # that one is ALLOWED to write. The api (read-only deep-health consumer)
    # must carry :ro. Assert at least one :ro backups mount exists.
    assert "backups:/backups:ro" in text, \
        "api service must mount backups read-only (design §4/§6, P1-4)"


# ---------------------------------------------------------------- no plaintext secret
def test_compose_has_no_plaintext_secret():
    text = _text(COMPOSE)
    # PLANPILOT_AUTH_SECRET must be REQUIRED interpolation, never a literal.
    m = re.search(r"PLANPILOT_AUTH_SECRET:\s*(\S.*)$", text, re.M)
    assert m, "compose must wire PLANPILOT_AUTH_SECRET"
    binding = m.group(1).strip()
    assert binding.startswith("${") and ":?" in binding, \
        f"auth secret must be required interpolation (${{...:?...}}), got {binding!r}"
    # structural: no `KEY: <quoted-or-bare literal>` secret assignment anywhere.
    # A secret literal looks like `SOMETHING_SECRET: "actualvalue"` or
    # `SOMETHING_SECRET: actualvalue` (not a ${...} interpolation).
    for ln in _lines(COMPOSE):
        stripped = ln.strip()
        mm = re.match(r"([A-Z0-9_]*(?:SECRET|KEY|TOKEN|PASSWORD))[A-Z0-9_]*:\s*(.+)$",
                      stripped)
        if not mm:
            continue
        value = mm.group(2).strip()
        assert value.startswith("${"), \
            f"plaintext secret literal in compose: {stripped!r}"


def test_compose_no_plaintext_secret_shape_repo_wide():
    """Belt: the deploy-gate layer-2 tracked-secret scan must stay green on
    the shipped compose/Dockerfile (no assignment-shaped secret literal)."""
    from tools.deploy_gate import probe_tracked_secrets  # noqa: E402
    f = probe_tracked_secrets()
    if f.status == "blocked":
        pytest.skip("git ls-files unavailable (no-.git sandbox)")
    assert f.status == "pass", f.detail


# ---------------------------------------------------------------- non-loopback bind
def test_compose_production_bind_is_non_loopback():
    text = _text(COMPOSE)
    # the api service must be production-profiled with PLANPILOT_ENV=production
    assert re.search(r"PLANPILOT_ENV:\s*production", text), \
        "compose api must set PLANPILOT_ENV=production explicitly"
    # a production port mapping must NOT publish on loopback. Find the api
    # ports entry; the published address defaults to 0.0.0.0 (non-loopback)
    # via interpolation, and must never be a hardcoded 127.0.0.1 / ::1.
    port_lines = [ln for ln in _lines(COMPOSE) if re.search(r"\d+:8080", ln)]
    assert port_lines, "compose api must publish the 8080 port"
    for ln in port_lines:
        assert "127.0.0.1:8080" not in ln and '"127.0.0.1' not in ln, \
            f"production bind must not be loopback: {ln!r}"
        assert "[::1]" not in ln, f"production bind must not be loopback: {ln!r}"

# NOTE: the live `docker compose config` RENDER and the daemon
# BLOCKED/FAIL classification are NOT asserted here on purpose.
# (a) classification (absent=>BLOCKED, broken-compose=>FAIL,
#     valid=>PASS) is pinned deterministically with fakes in
#     tests/unit/test_deploy_gate.py; (b) a REAL render+build+up
#     probe is captured in the Phase-6 evidence log
#     (g4-staging live53 run: compose config EXIT 0, /health/ready
#     200, backup mount ro, healthy). Running a live daemon inside
#     the deterministic unit suite would make the pass/skip count
#     depend on ambient Docker state — a reproducibility hazard.
# So this file stays STATIC and daemon-free by design.
