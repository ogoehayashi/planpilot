#!/usr/bin/env python3
"""Fail closed when the P1-1 report, evidence or binding surfaces drift."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tests/evidence/model-binding/EVIDENCE.json"
REPORT = ROOT / "P1_1_MODEL_BINDING_REMEDIATION_REPORT_20260918.md"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"
PINNED_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"


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
    check(data.get("live_bedrock_called") is False, "offline evidence made no live Bedrock call")
    check(actual_contract == data.get("contract_sha256") == EXPECTED_SHA, "contract SHA is pinned")
    check(data.get("binding") == {
        "service": "AWS Bedrock", "region": "ap-southeast-1", "model": PINNED_MODEL,
    }, "runtime evidence has the exact contract binding")
    check(data.get("package_manifest_binding") == {
        "default_region": "ap-southeast-1", "default_model": PINNED_MODEL,
    }, "generated package manifest has the exact binding")
    for path, expected in data["subject_sha256"].items():
        check(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected,
              f"subject hash matches {path}")
    results = data["results"]
    check("47 passed" in results["focused"]["summary"], "47 focused tests")
    check("609 passed" in results["unit"]["summary"], "609 unit tests")
    check("616 passed" in results["full"]["summary"], "616 full tests")
    check(data["negative_control"] == {"caught": 6, "escaped": 0, "broken": 0, "total": 6},
          "6/6 model-binding mutations caught")
    check(data["formal_eval"] == {"passed": 0, "failed": 0, "blocked": 30},
          "formal EVAL remains 0/0/30")
    check("609 passed" in report and "616 passed" in report and "0 escaped" in report,
          "report uses measured counts")
    check("0 PASS / 0 FAIL / 30 BLOCKED" in report, "report does not claim formal EVAL")
    tasks = (ROOT / ".kiro/specs/model-binding/tasks.md").read_text(encoding="utf-8")
    check(tasks.count("- [ ]") == 2, "only the two live deployment checks remain open")
    print(f"MODEL BINDING HANDOFF FACT-CHECK | fails={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
