#!/usr/bin/env python3
"""Generate reproducible P0-6 evidence without claiming formal EVAL completion."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/evidence/security-audit"
CONTRACT = ROOT / "contract/planpilot_agent_contract_v1.8.json"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"
SUBJECTS = (
    "src/planpilot/audit.py", "src/planpilot/persistence.py",
    "src/planpilot/tools/middleware.py", "src/planpilot/workflow/events.py",
    "src/planpilot/factory_state.py", "src/planpilot/runtime_planning.py",
    "src/planpilot/independent_validator.py", "src/planpilot/agent/chat.py",
    "tools/api_server.py", "tools/web/index.html", "tools/check_web_render.cjs",
    "tests/unit/test_security_audit.py", "tests/unit/test_event_workflow.py",
    "tests/negative_control/test_security_audit_negctl.py",
    ".kiro/specs/audit-hash-chain/design.md", ".kiro/specs/audit-hash-chain/tasks.md",
    ".kiro/specs/decision-traces/design.md", ".kiro/specs/decision-traces/tasks.md",
    "P0_6_SECURITY_AUDIT_REMEDIATION_REPORT_20260918.md",
    "tools/write_evidence_security_audit.py", "tools/factcheck_security_audit_handoff.py",
)


def run(name: str, args: list[str], *, input_text: str | None = None, timeout: int = 1200) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(
        args, cwd=ROOT, env=env, input=input_text, capture_output=True, text=True,
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
    node = shutil.which("node") or str(
        Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
    )
    if not Path(node).is_file():
        raise SystemExit("node executable not found; set PATH to the workspace Node.js runtime")
    sys.path.insert(0, str(ROOT / "src"))
    from planpilot.domain.importer import load_factory
    from planpilot.domain.planning import build_candidates
    candidates = json.dumps([
        asdict(item) for item in build_candidates(load_factory(ROOT / "examples/factory_demo.json"))
    ], ensure_ascii=False)
    results = {
        "focused": run("focused", [py, "-m", "pytest", "tests/unit/test_security_audit.py",
                                    "tests/unit/test_event_workflow.py", "-q"]),
        "negative_control": run("negative_control", [py, "-m", "pytest",
            "tests/negative_control/test_security_audit_negctl.py", "-q", "-s"]),
        "p0_5_negative_regression": run("p0_5_negative_regression", [py, "-m", "pytest",
            "tests/negative_control/test_tool_error_middleware_negctl.py", "-q", "-s"]),
        "unit": run("unit", [py, "-m", "pytest", "tests/unit", "-q"]),
        "full": run("full", [py, "-m", "pytest", "tests", "-q"]),
        "closed_vocabularies": run("closed_vocabularies", [py, "tools/check_closed_vocabularies.py"]),
        "closed_vocabularies_self_test": run(
            "closed_vocabularies_self_test", [py, "tools/check_closed_vocabularies.py", "--self-test"]),
        "workspace": run("workspace", [py, "tools/validate_kiro_workspace.py"]),
        "web_render": run("web_render", [node, "tools/check_web_render.cjs"], input_text=candidates),
        "compile": run("compile", [py, "-m", "compileall", "-q", "src", "tools/api_server.py"]),
        "formal_eval_readiness": run("formal_eval_readiness", [py, "tools/run_evals.py"]),
    }
    neg_text = (OUT / "negative_control.log").read_text(encoding="utf-8")
    match = re.search(r"caught=(\d+) escaped=(\d+) broken=(\d+) of (\d+)", neg_text)
    negative = None if match is None else {
        "caught": int(match.group(1)), "escaped": int(match.group(2)),
        "broken": int(match.group(3)), "total": int(match.group(4)),
    }
    p0_5_text = (OUT / "p0_5_negative_regression.log").read_text(encoding="utf-8")
    p0_5_match = re.search(r"caught=(\d+) escaped=(\d+) broken=(\d+) of (\d+)", p0_5_text)
    p0_5_negative = None if p0_5_match is None else {
        "caught": int(p0_5_match.group(1)), "escaped": int(p0_5_match.group(2)),
        "broken": int(p0_5_match.group(3)), "total": int(p0_5_match.group(4)),
    }
    normal_green = all(
        result["exit_code"] == 0 for name, result in results.items()
        if name != "formal_eval_readiness"
    )
    eval_log = (OUT / "formal_eval_readiness.log").read_text(encoding="utf-8")
    all_green = (
        normal_green
        and results["formal_eval_readiness"]["exit_code"] == 1
        and "cases=30 passed=0 failed=0 blocked=30" in eval_log
        and negative == {"caught": 9, "escaped": 0, "broken": 0, "total": 9}
        and p0_5_negative == {"caught": 11, "escaped": 0, "broken": 0, "total": 11}
    )
    evidence = {
        "evidence_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "module": "security-audit-and-decision-traces",
        "contract_sha256": contract_sha,
        "repository_form": "restored archive without .git metadata",
        "subject_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS
        },
        "results": results,
        "negative_control": negative,
        "p0_5_negative_regression": p0_5_negative,
        "formal_eval": {"passed": 0, "failed": 0, "blocked": 30},
        "all_green": all_green,
        "scope_statement": (
            "P0-6 implementation evidence only. The remaining seven business handlers, publisher, "
            "Bedrock, deployment and formal EVAL-001..030 execution are outside scope."
        ),
    }
    (OUT / "EVIDENCE.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"SECURITY AUDIT EVIDENCE | all_green={all_green} | {OUT / 'EVIDENCE.json'}")
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
