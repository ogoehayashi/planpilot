#!/usr/bin/env python3
"""Generate offline P1-1 model-binding evidence without calling Bedrock."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/evidence/model-binding"
CONTRACT = ROOT / "contract/planpilot_agent_contract_v1.8.json"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"
PINNED_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
SUBJECTS = (
    "src/planpilot/inference/bedrock_client.py", "src/planpilot/agent/bedrock.py",
    "tests/unit/test_bedrock_web.py", "tests/negative_control/test_model_binding_negctl.py",
    "tools/start_local.ps1", "tools/build_team_package.py", "tools/generate_kiro_workspace.py",
    ".env.example", "Dockerfile", "compose.yaml", "START_HERE.md", "PRODUCT_RUNBOOK.md",
    "docs/bedrock-web-guide.md", ".kiro/specs/bedrock-web/design.md",
    ".kiro/specs/bedrock-web/tasks.md", ".kiro/specs/model-binding/design.md",
    ".kiro/specs/model-binding/tasks.md", "P1_1_MODEL_BINDING_REMEDIATION_REPORT_20260918.md",
    "tools/write_evidence_model_binding.py", "tools/factcheck_model_binding_handoff.py",
)


def run(name: str, args: list[str], timeout: int = 1200) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PLANPILOT_FORBID_LLM_NETWORK="1")
    result = subprocess.run(
        args, cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    output = (result.stdout + result.stderr).replace("\r\n", "\n")
    (OUT / f"{name}.log").write_text(output, encoding="utf-8", newline="\n")
    return {
        "command": args, "exit_code": result.returncode,
        "summary": output.strip().splitlines()[-1] if output.strip() else "",
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    contract_sha = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    if contract_sha != EXPECTED_SHA:
        raise SystemExit(f"contract drift: {contract_sha}")
    py = sys.executable
    results = {
        "focused": run("focused", [py, "-m", "pytest", "tests/unit/test_bedrock_web.py",
                                    "tests/unit/test_requirement_delivery.py", "-q"]),
        "negative_control": run("negative_control", [py, "-m", "pytest",
            "tests/negative_control/test_model_binding_negctl.py", "-q", "-s"]),
        "unit": run("unit", [py, "-m", "pytest", "tests/unit", "-q"]),
        "full": run("full", [py, "-m", "pytest", "tests", "-q"]),
        "closed_vocabularies": run("closed_vocabularies", [py, "tools/check_closed_vocabularies.py"]),
        "closed_vocabularies_self_test": run(
            "closed_vocabularies_self_test", [py, "tools/check_closed_vocabularies.py", "--self-test"]),
        "workspace": run("workspace", [py, "tools/validate_kiro_workspace.py"]),
        "compile": run("compile", [py, "-m", "compileall", "-q", "src", "tools"]),
        "formal_eval_readiness": run("formal_eval_readiness", [py, "tools/run_evals.py"]),
    }
    with tempfile.TemporaryDirectory(prefix="planpilot-model-binding-") as folder:
        package = Path(folder) / "PlanPilot-handoff.zip"
        results["package"] = run(
            "package", [py, "tools/build_team_package.py", "--output", str(package)]
        )
        package_manifest = None
        if results["package"]["exit_code"] == 0:
            with zipfile.ZipFile(package) as archive:
                package_manifest = json.loads(
                    archive.read("PlanPilot/PACKAGE_MANIFEST.json").decode("utf-8")
                )
    neg_text = (OUT / "negative_control.log").read_text(encoding="utf-8")
    match = re.search(r"caught=(\d+) escaped=(\d+) broken=(\d+) of (\d+)", neg_text)
    negative = None if match is None else {
        "caught": int(match.group(1)), "escaped": int(match.group(2)),
        "broken": int(match.group(3)), "total": int(match.group(4)),
    }
    normal_green = all(
        value["exit_code"] == 0 for key, value in results.items()
        if key != "formal_eval_readiness"
    )
    eval_log = (OUT / "formal_eval_readiness.log").read_text(encoding="utf-8")
    all_green = (
        normal_green
        and results["formal_eval_readiness"]["exit_code"] == 1
        and "cases=30 passed=0 failed=0 blocked=30" in eval_log
        and negative == {"caught": 6, "escaped": 0, "broken": 0, "total": 6}
        and package_manifest is not None
        and package_manifest.get("default_region") == "ap-southeast-1"
        and package_manifest.get("default_model") == PINNED_MODEL
    )
    evidence = {
        "evidence_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "module": "contract-model-binding",
        "contract_sha256": contract_sha,
        "repository_form": "restored archive without .git metadata",
        "binding": {"service": "AWS Bedrock", "region": "ap-southeast-1", "model": PINNED_MODEL},
        "live_bedrock_called": False,
        "subject_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS
        },
        "results": results,
        "negative_control": negative,
        "package_manifest_binding": None if package_manifest is None else {
            "default_region": package_manifest.get("default_region"),
            "default_model": package_manifest.get("default_model"),
        },
        "formal_eval": {"passed": 0, "failed": 0, "blocked": 30},
        "all_green": all_green,
        "scope_statement": (
            "P1-1 offline binding evidence only. Team-account permission, live Bedrock response, "
            "Lightsail, pricing reconciliation and formal EVAL execution remain unverified."
        ),
    }
    (OUT / "EVIDENCE.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"MODEL BINDING EVIDENCE | all_green={all_green} | {OUT / 'EVIDENCE.json'}")
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
