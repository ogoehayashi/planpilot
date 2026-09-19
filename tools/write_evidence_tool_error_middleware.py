#!/usr/bin/env python3
"""Generate reproducible implementation evidence for P0-5; never claims EVAL."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/evidence/tool-error-middleware"
CONTRACT = ROOT / "contract/planpilot_agent_contract_v1.8.json"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"
SUBJECTS = (
    "src/planpilot/tools/__init__.py", "src/planpilot/tools/correlation.py",
    "src/planpilot/tools/errors.py", "src/planpilot/tools/middleware.py",
    "src/planpilot/tools/registry.py", "src/planpilot/tools/serialization.py",
    "src/planpilot/tools/transaction.py", "src/planpilot/engine.py",
    "tests/unit/test_tool_error_middleware.py",
    "tests/negative_control/test_tool_error_middleware_negctl.py",
    ".kiro/specs/tool-error-middleware/tasks.md",
    "P0_5_TOOL_ERROR_MIDDLEWARE_REPORT_20260918.md",
    "tools/write_evidence_tool_error_middleware.py",
    "tools/factcheck_tool_error_handoff.py",
)


def run(name: str, args: list[str], timeout: int = 1200) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    output = (result.stdout + result.stderr).replace("\r\n", "\n")
    (OUT / f"{name}.log").write_text(output, encoding="utf-8", newline="\n")
    return {"command": args, "exit_code": result.returncode,
            "summary": output.strip().splitlines()[-1] if output.strip() else ""}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    contract_sha = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    if contract_sha != EXPECTED_SHA:
        raise SystemExit(f"contract drift: {contract_sha}")
    py = sys.executable
    results = {
        "focused": run("focused", [py, "-m", "pytest",
            "tests/unit/test_tool_error_middleware.py", "tests/unit/test_engine_utilities.py", "-q"]),
        "negative_control": run("negative_control", [py, "-m", "pytest",
            "tests/negative_control/test_tool_error_middleware_negctl.py", "-q", "-s"]),
        "unit": run("unit", [py, "-m", "pytest", "tests/unit", "-q"]),
        "full": run("full", [py, "-m", "pytest", "tests", "-q"]),
        "closed_vocabularies": run("closed_vocabularies", [py, "tools/check_closed_vocabularies.py"]),
        "closed_vocabularies_self_test": run("closed_vocabularies_self_test",
                                               [py, "tools/check_closed_vocabularies.py", "--self-test"]),
        "workspace": run("workspace", [py, "tools/validate_kiro_workspace.py"]),
        "compile": run("compile", [py, "-m", "compileall", "-q", "src/planpilot/tools"]),
    }
    neg_text = (OUT / "negative_control.log").read_text(encoding="utf-8")
    match = re.search(r"caught=(\d+) escaped=(\d+) broken=(\d+) of (\d+)", neg_text)
    negative = None if match is None else {
        "caught": int(match.group(1)), "escaped": int(match.group(2)),
        "broken": int(match.group(3)), "total": int(match.group(4)),
    }
    all_green = all(item["exit_code"] == 0 for item in results.values()) and negative == {
        "caught": 11, "escaped": 0, "broken": 0, "total": 11,
    }
    evidence = {
        "evidence_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "module": "tool-error-middleware", "contract_sha256": contract_sha,
        "repository_form": "restored archive without .git metadata",
        "subject_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SUBJECTS},
        "results": results, "negative_control": negative, "all_green": all_green,
        "reference_failure_wire": {
            "utf8_hex": (OUT / "reference_failure_wire.hex").read_text(encoding="utf-8").strip()
            if (OUT / "reference_failure_wire.hex").exists() else None,
        },
        "scope_statement": (
            "P0-5 implementation evidence only. Public business handlers, publisher, audit-chain, "
            "decision-trace persistence, Bedrock, deployment and EVAL-001..030 are outside scope."
        ),
    }
    sample = {
        "correlation_id": "12345678-1234-4234-9234-123456789abc",
        "details": {"limit_scope": "tool", "retry_after_seconds": 3},
        "error_code": "RATE_LIMITED", "message": "Request rate exceeded.", "retryable": True,
    }
    wire = json.dumps(sample, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (OUT / "reference_failure_wire.hex").write_text(wire.hex() + "\n", encoding="utf-8")
    evidence["reference_failure_wire"] = {"utf8_hex": wire.hex(), "byte_length": len(wire)}
    (OUT / "EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
    print(f"TOOL MIDDLEWARE EVIDENCE | all_green={all_green}")
    print(f"output={OUT}")
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())

