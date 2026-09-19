#!/usr/bin/env python3
"""Fail closed when the P0-6 report, tasks, counts, or hashed subjects drift."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tests/evidence/security-audit/EVIDENCE.json"
REPORT = ROOT / "P0_6_SECURITY_AUDIT_REMEDIATION_REPORT_20260918.md"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"


def main() -> int:
    failures = []

    def check(value, message):
        print(f"  {'OK' if value else 'FAIL'} | {message}")
        if not value:
            failures.append(message)

    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")
    actual_contract = hashlib.sha256(
        (ROOT / "contract/planpilot_agent_contract_v1.8.json").read_bytes()
    ).hexdigest()
    check(data.get("all_green") is True, "evidence is green")
    check(actual_contract == data.get("contract_sha256") == EXPECTED_SHA, "contract SHA is pinned")
    for path, expected in data["subject_sha256"].items():
        check(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected,
              f"subject hash matches {path}")
    results = data["results"]
    check("13 passed" in results["focused"]["summary"], "13 focused tests")
    check("604 passed" in results["unit"]["summary"], "604 unit tests")
    check("610 passed" in results["full"]["summary"], "610 full tests")
    check(data["negative_control"] == {"caught": 9, "escaped": 0, "broken": 0, "total": 9},
          "9/9 P0-6 mutations caught")
    check(data["p0_5_negative_regression"] == {
        "caught": 11, "escaped": 0, "broken": 0, "total": 11,
    }, "11/11 P0-5 mutations remain caught")
    check(data["formal_eval"] == {"passed": 0, "failed": 0, "blocked": 30},
          "formal EVAL remains 0/0/30")
    check("604 passed" in report and "610 passed" in report and "0 escaped" in report,
          "report uses measured counts")
    check("0 PASS / 0 FAIL / 30 BLOCKED" in report, "report does not claim formal EVAL")
    for path in (
        ".kiro/specs/audit-hash-chain/tasks.md",
        ".kiro/specs/decision-traces/tasks.md",
    ):
        check("- [ ]" not in (ROOT / path).read_text(encoding="utf-8"), f"all tasks checked: {path}")
    check("quarantine_impact" in (ROOT / "tools/web/index.html").read_text(encoding="utf-8"),
          "UI exposes quarantine impact")
    print(f"SECURITY AUDIT HANDOFF FACT-CHECK | fails={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
