#!/usr/bin/env python3
"""Generate reproducible, timestamped approval-service module evidence.

This is implementation evidence only.  It does not execute EVAL-001..030 and
must not be used to claim pilot readiness.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "contract").is_dir() and (parent / "src" / "planpilot").is_dir():
            return parent
    raise SystemExit("repository root not found")


ROOT = _root()
OUT = ROOT / "tests" / "evidence" / "approval-service"
CONTRACT = ROOT / "contract" / "planpilot_agent_contract_v1.8.json"
EXPECTED_CONTRACT_SHA256 = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"
SUBJECTS = (
    "src/planpilot/approval/__init__.py",
    "src/planpilot/approval/errors.py",
    "src/planpilot/approval/policy.py",
    "src/planpilot/approval/service.py",
    "src/planpilot/validation/__init__.py",
    "src/planpilot/validation/schema.py",
    "tests/unit/test_approval_service.py",
    "tests/negative_control/test_approval_service_negctl.py",
    ".kiro/specs/approval-service/design.md",
    ".kiro/specs/approval-service/tasks.md",
    "tools/write_evidence_approval_service.py",
    "tools/factcheck_approval_handoff.py",
)


def _run(name: str, args: list[str], *, timeout: int = 1200) -> dict:
    result = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = (result.stdout + result.stderr).replace("\r\n", "\n")
    (OUT / f"{name}.log").write_text(output, encoding="utf-8", newline="\n")
    summary = output.strip().splitlines()[-1] if output.strip() else ""
    return {
        "command": args,
        "exit_code": result.returncode,
        "summary": summary,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    contract_sha = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    if contract_sha != EXPECTED_CONTRACT_SHA256:
        raise SystemExit(f"contract drift: {contract_sha}")

    py = sys.executable
    results = {
        "approval_unit": _run(
            "approval_unit",
            [py, "-m", "pytest", "tests/unit/test_approval_service.py", "-q"],
        ),
        "approval_negative_control": _run(
            "approval_negative_control",
            [
                py,
                "-m",
                "pytest",
                "tests/negative_control/test_approval_service_negctl.py",
                "-q",
                "-s",
            ],
        ),
        "closed_vocabularies": _run(
            "closed_vocabularies", [py, "tools/check_closed_vocabularies.py"]
        ),
        "workspace_validation": _run(
            "workspace_validation", [py, "tools/validate_kiro_workspace.py"]
        ),
        "full_suite": _run("full_suite", [py, "-m", "pytest", "tests", "-q"]),
        "diff_check": _run("diff_check", ["git", "diff", "--check"]),
    }

    neg_text = (OUT / "approval_negative_control.log").read_text(encoding="utf-8")
    match = re.search(
        r"APPROVAL NEGATIVE CONTROL \| caught=(\d+) escaped=(\d+) broken=(\d+) of (\d+)",
        neg_text,
    )
    negative_control = None
    if match:
        caught, escaped, broken, total = (int(value) for value in match.groups())
        negative_control = {
            "caught": caught,
            "escaped": escaped,
            "broken": broken,
            "total": total,
            "all_caught": caught == total and escaped == 0 and broken == 0,
        }

    hashes = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS
    }
    all_green = (
        all(result["exit_code"] == 0 for result in results.values())
        and negative_control is not None
        and negative_control["all_caught"]
    )
    evidence = {
        "evidence_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "module": "approval-service",
        "contract": {
            "path": str(CONTRACT.relative_to(ROOT)).replace("\\", "/"),
            "sha256": contract_sha,
        },
        "python": sys.version,
        "subject_sha256": hashes,
        "results": results,
        "negative_control": negative_control,
        "all_green": all_green,
        "scope_statement": (
            "Module/unit and mutation evidence only. Scheduler, public tool adapters, "
            "publish transaction, audit chain, UI, dataset integration, and EVAL-001..030 "
            "remain outside this evidence and runtime evaluation remains pending."
        ),
    }
    (OUT / "EVIDENCE.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"APPROVAL EVIDENCE | all_green={all_green}")
    print(f"output={OUT}")
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
