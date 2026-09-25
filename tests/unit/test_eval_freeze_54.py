"""G4 5.4 — EVAL truth locks: run_evals.py is frozen; smoke stays separate.

These tests do NOT change the eval pipeline (tasks 5.4: `run_evals.py`
byte-unchanged, no bypass switch). They pin the FROZEN facts:
  * the exact SHA-256 of run_evals.py and run_smoke_harness.py,
  * the real 30-BLOCKED / 0-PASS / exit-1 summary shape,
  * smoke's default output lives in tests/evidence/compact-smoke
    (compact-smoke != runtime-eval), and smoke still REFUSES to write
    under the formal runtime-eval dir,
  * neither script grew a bypass switch (no `--allow-pass`, no env that
    promotes BLOCKED to PASS): the argparse surface is pinned too.
"""
import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Pin the EXACT bytes. Any change to either file MUST come with a
# reviewer-approved spec update that re-derives these hashes — updating
# the hash alone to make a test pass is exactly the bypass we forbid.
RUN_EVALS_SHA256 = "6285b78205619876125ede12841fd46a26eb8c79f6b54b3136d5260ca3707932"
RUN_SMOKE_SHA256 = "47da9ba988bacf4e7b0e4fa79f929b8711c5825ccb26e114749501e1a849ba26"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_run_evals_is_byte_frozen():
    assert _sha(ROOT / "tools/run_evals.py") == RUN_EVALS_SHA256, (
        "run_evals.py changed — tasks 5.4 freezes it; any edit needs a "
        "reviewer-approved spec update first")


def test_smoke_harness_is_byte_frozen():
    assert _sha(ROOT / "tools/run_smoke_harness.py") == RUN_SMOKE_SHA256, (
        "run_smoke_harness.py changed — its runtime-eval WRITE FORBIDEN "
        "guard (line ~82) and compact-smoke default (line ~22) are "
        "spec-locked by design §6")


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # pre-register: dataclass/annotation resolution
    spec.loader.exec_module(module)
    return module


# ---------- summary shape: the REAL acceptance numbers ----------
def test_formal_summary_lines_reassert_blocked_shape(tmp_path, capsys):
    runner = load_tool("run_evals")
    rc = runner.main(["--output", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "cases=30 passed=0 failed=0 blocked=30" in out
    ev = json.loads((tmp_path / "EVIDENCE.json").read_text(encoding="utf-8"))
    assert (ev["passed"], ev["failed"], ev["blocked"]) == (0, 0, 30)


# ---------- output separation: smoke vs formal ----------
def test_smoke_default_output_is_compact_smoke_not_runtime_eval():
    smoke = load_tool("run_smoke_harness")
    assert smoke.OUT == ROOT / "tests" / "evidence" / "compact-smoke"
    formal = ROOT / "tests" / "evidence" / "runtime-eval"
    assert smoke.OUT != formal
    assert smoke.OUT.name != "runtime-eval"  # rev.4 wrongly wrote runtime-eval


def test_smoke_still_refuses_formal_dir(tmp_path):
    smoke = load_tool("run_smoke_harness")
    with pytest.raises(ValueError):
        smoke.run(ROOT / "tests" / "evidence" / "runtime-eval")


# ---------- no bypass switch may appear ----------
def _argparse_flags(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {seg.strip("'\",") for seg in text.replace("(", " ").split()
            if seg.strip("'\",").startswith("--")}


@pytest.mark.parametrize("tool,locked", [
    ("run_evals.py", {"--output"}),
    ("run_smoke_harness.py", {"--output"}),
])
def test_only_the_known_flag_surface_exists(tool, locked):
    flags = _argparse_flags(ROOT / "tools" / tool)
    # every flag must be the documented --output; nothing that could
    # promote BLOCKED->PASS, skip real AWS, or silence evidence
    assert flags == locked, f"{tool} grew flags: {sorted(flags - locked)}"


def test_run_evals_exit_rule_has_no_second_door():
    src = (ROOT / "tools/run_evals.py").read_text(encoding="utf-8")
    # exit 0 ONLY when passed==case_count and zero failed/blocked; the
    # line is pinned verbatim so a bypass edit trips this test
    assert ('return 0 if result["case_count"] > 0 and result["passed"] == result["case_count"] and not (result["failed"] or result["blocked"]) else 1'
            in src)
