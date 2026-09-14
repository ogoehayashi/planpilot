#!/usr/bin/env python3
"""Fail closed when the approval-service handoff drifts from its evidence/tree."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path


def _root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "contract").is_dir() and (parent / ".git").exists():
            return parent
    raise SystemExit("repository root not found")


ROOT = _root()
EVIDENCE = ROOT / "tests/evidence/approval-service/EVIDENCE.json"
REPORT = ROOT / "APPROVAL_SERVICE_HANDOFF.md"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"


def main() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        print(f"  {'OK' if condition else 'FAIL'} | {message}")
        if not condition:
            failures.append(message)

    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")
    contract_sha = hashlib.sha256(
        (ROOT / "contract/planpilot_agent_contract_v1.8.json").read_bytes()
    ).hexdigest()

    print("=== approval-service evidence ===")
    check(data.get("all_green") is True, "evidence all_green is true")
    check(contract_sha == EXPECTED_SHA == data["contract"]["sha256"], "contract SHA is pinned")
    for path, expected in data["subject_sha256"].items():
        actual = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        check(actual == expected, f"evidence hash matches {path}")

    results = data["results"]
    check(re.search(r"39 passed", results["approval_unit"]["summary"]) is not None, "39 approval tests")
    neg = data["negative_control"]
    check(neg == {"caught": 13, "escaped": 0, "broken": 0, "total": 13, "all_caught": True}, "13/13 mutations caught")
    check(re.search(r"461 passed", results["full_suite"]["summary"]) is not None, "461 full-suite tests")
    check(results["closed_vocabularies"]["summary"] == "CLOSED VOCABULARY CHECK | PASS", "vocabulary guard passed")
    check(results["workspace_validation"]["summary"] == "WORKSPACE VALIDATION | ok=190 fail=0", "workspace validation passed")

    print("\n=== handoff and git baseline ===")
    tasks = (ROOT / ".kiro/specs/approval-service/tasks.md").read_text(encoding="utf-8")
    check(re.search(r"^- \[ \]", tasks, re.MULTILINE) is None, "approval tasks contain no unchecked item")
    for phrase in (
        "39 passed",
        "13 caught / 0 escaped / 0 broken",
        "461 passed",
        "pilot-ready",
    ):
        check(phrase in report, f"handoff contains {phrase!r}")

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    try:
        tagged = subprocess.check_output(
            ["git", "rev-parse", "plan-store-and-digest-v1-baseline^{}"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        check(tagged == "c7c06ea89a19c12984aa3d1a9c021a4e4e574f5c", "parent annotated tag peels to c7c06ea")
    except subprocess.CalledProcessError:
        check(False, "parent baseline tag exists")
    try:
        approval_tag = subprocess.check_output(
            ["git", "rev-parse", "approval-service-v1-baseline^{}"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        check(approval_tag == head, "approval annotated tag peels to HEAD")
    except subprocess.CalledProcessError:
        check(False, "approval baseline tag exists")
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    check(status == "", "working tree is clean")

    print(f"\nAPPROVAL HANDOFF FACT-CHECK | fails={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
