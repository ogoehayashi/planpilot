#!/usr/bin/env python3
"""Capture reproducible offline P1-2 implementation evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/evidence/p1-2-scheduling-engine"
CONTRACT = ROOT / "contract/planpilot_agent_contract_v1.8.json"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"
SUBJECTS = (
    "src/planpilot/domain/calendar.py",
    "src/planpilot/domain/objectives.py",
    "src/planpilot/domain/lots.py",
    "src/planpilot/domain/models.py",
    "src/planpilot/domain/importer.py",
    "src/planpilot/domain/planning.py",
    "src/planpilot/runtime_planning.py",
    "src/planpilot/v18_adapter.py",
    "src/planpilot/independent_validator.py",
    "tests/unit/test_p1_2_engine.py",
    ".kiro/specs/p1-2-scheduling-engine/design.md",
    ".kiro/specs/p1-2-scheduling-engine/tasks.md",
)


def run(name: str, command: list[str]) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PLANPILOT_FORBID_LLM_NETWORK="1")
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=1200)
    output = (result.stdout + result.stderr).replace("\r\n", "\n")
    (OUT / f"{name}.log").write_text(output, encoding="utf-8", newline="\n")
    match = re.search(r"(\d+) passed", output)
    return {"command": command, "exit_code": result.returncode,
            "passed": None if match is None else int(match.group(1)),
            "log_sha256": hashlib.sha256(output.encode()).hexdigest()}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    contract_hash = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    if contract_hash != EXPECTED_SHA:
        raise SystemExit("contract hash drift")
    py = sys.executable
    results = {
        "focused": run("focused", [py, "-m", "pytest", "tests/unit/test_p1_2_engine.py", "-q"]),
        "unit": run("unit", [py, "-m", "pytest", "tests/unit", "-q"]),
        "full": run("full", [py, "-m", "pytest", "tests", "-q"]),
        "compile": run("compile", [py, "-m", "compileall", "-q", "src", "tools"]),
        "closed_vocabularies": run("closed_vocabularies", [py, "tools/check_closed_vocabularies.py"]),
        "workspace": run("workspace", [py, "tools/validate_kiro_workspace.py"]),
        "formal_eval_readiness": run("formal_eval_readiness", [py, "tools/run_evals.py"]),
    }
    formal_log = (OUT / "formal_eval_readiness.log").read_text(encoding="utf-8")
    checks_green = all(row["exit_code"] == 0 for name, row in results.items()
                       if name != "formal_eval_readiness")
    formal_blocked = (results["formal_eval_readiness"]["exit_code"] == 1
                      and "cases=30 passed=0 failed=0 blocked=30" in formal_log)
    evidence = {
        "evidence_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "module": "p1-2-scheduling-engine",
        "contract_sha256": contract_hash,
        "repository_form": "restored archive without .git metadata",
        "subject_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS
        },
        "results": results,
        "formal_eval": {"passed": 0, "failed": 0, "blocked": 30},
        "all_green_offline": checks_green and formal_blocked,
        "scope_statement": (
            "Offline deterministic implementation checks only. Formal EVAL, full 40-second "
            "escalation, cross-state published-baseline discovery and large-factory SLA remain unverified."
        ),
    }
    (OUT / "EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    print(f"P1-2 EVIDENCE | all_green_offline={evidence['all_green_offline']} | "
          f"focused={results['focused']['passed']} unit={results['unit']['passed']} "
          f"full={results['full']['passed']} formal=0/0/30")
    return 0 if evidence["all_green_offline"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
