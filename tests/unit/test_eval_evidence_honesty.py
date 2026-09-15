"""Regression tests that keep component smoke separate from formal acceptance."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contract" / "planpilot_agent_contract_v1.8.json"


def _load_tool(name: str):
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_formal_gate_preserves_exact_contract_cases_and_blocks_all(tmp_path):
    tool = _load_tool("run_evals")
    evidence = tool.write_evidence(tmp_path)
    contract_cases = json.loads(CONTRACT.read_text(encoding="utf-8"))["acceptance_tests"]

    assert evidence["case_count"] == 30
    assert (evidence["passed"], evidence["failed"], evidence["blocked"]) == (0, 0, 30)
    assert evidence["acceptance_ready"] is False
    assert evidence["acceptance_claimed"] is False
    assert evidence["runtime_evaluation"] == "PENDING_UNTIL_EVAL_001_TO_030_EXECUTE"
    assert [result["case_id"] for result in evidence["results"]] == [case["case_id"] for case in contract_cases]
    assert [result["pass_condition"] for result in evidence["results"]] == [case["pass_condition"] for case in contract_cases]
    assert all(result["status"] == "BLOCKED" for result in evidence["results"])
    assert all(result["unmet_pass_condition"] and result["evidence_refs"] == [] for result in evidence["results"])


def test_formal_gate_cli_is_fail_closed_but_still_writes_evidence(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "run_evals.py"), "--output", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    evidence = json.loads((tmp_path / "EVIDENCE.json").read_text(encoding="utf-8"))
    assert evidence["blocked"] == 30
    assert evidence["acceptance_ready"] is False
    assert "NOT READY" in completed.stdout


def test_component_smoke_has_no_formal_case_results(tmp_path):
    tool = _load_tool("run_smoke_harness")
    evidence = tool.run(tmp_path)

    assert evidence["evidence_kind"] == "component_smoke"
    assert evidence["smoke_passed"] == evidence["check_count"]
    assert evidence["smoke_failed"] == 0
    assert evidence["acceptance_claimed"] is False
    assert evidence["runtime_evaluation"] == "PENDING_UNTIL_EVAL_001_TO_030_EXECUTE"
    assert all("check_id" in result and "case_id" not in result for result in evidence["results"])
    assert all(result["status"] == "pass" for result in evidence["results"])


def test_current_docs_do_not_claim_formal_acceptance():
    current_docs = [
        ROOT / "START_HERE.md",
        ROOT / "PRODUCT_RUNBOOK.md",
        ROOT / "PROJECT_OVERVIEW_BILINGUAL.md",
        ROOT / "tests" / "evidence" / "runtime-eval" / "README.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in current_docs)
    assert "30 PASS" not in text
    assert "30/30 PASS" not in text


def test_delivery_helpers_cover_extracted_package_hazards():
    builder = _load_tool("build_team_package")
    assert builder.included(Path("PACKAGE_MANIFEST.json")) is False
    assert builder.included(Path("tests/evidence/example/run.log")) is True
    assert "'-X', 'utf8'" in (ROOT / "tools" / "setup_local.ps1").read_text(encoding="utf-8")
    for path in (
        ROOT / "tests" / "negative_control" / "test_plan_store_negctl.py",
        ROOT / "tests" / "negative_control" / "test_approval_service_negctl.py",
    ):
        assert 'startswith(".venv")' in path.read_text(encoding="utf-8")
