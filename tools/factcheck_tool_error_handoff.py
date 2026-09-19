#!/usr/bin/env python3
"""Fail closed when the P0-5 report, task state, or hashed subjects drift."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tests/evidence/tool-error-middleware/EVIDENCE.json"
REPORT = ROOT / "P0_5_TOOL_ERROR_MIDDLEWARE_REPORT_20260918.md"
EXPECTED_SHA = "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639"


def main() -> int:
    failures = []
    def check(value, message):
        print(f"  {'OK' if value else 'FAIL'} | {message}")
        if not value:
            failures.append(message)

    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")
    actual_contract = hashlib.sha256((ROOT / "contract/planpilot_agent_contract_v1.8.json").read_bytes()).hexdigest()
    check(data.get("all_green") is True, "evidence is green")
    check(actual_contract == data.get("contract_sha256") == EXPECTED_SHA, "contract SHA is pinned")
    for path, expected in data["subject_sha256"].items():
        check(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected,
              f"subject hash matches {path}")
    summaries = data["results"]
    check("35 passed" in summaries["focused"]["summary"], "35 focused tests")
    check("604 passed" in summaries["unit"]["summary"], "604 unit tests")
    check("610 passed" in summaries["full"]["summary"], "610 full tests")
    check(data["negative_control"] == {"caught": 11, "escaped": 0, "broken": 0, "total": 11},
          "11/11 mutations caught")
    check("610 passed" in report and "0 escaped" in report, "report uses measured counts")
    check("EVAL-001～030" in report and "仍未执行" in report, "report does not claim formal EVAL")
    tasks = (ROOT / ".kiro/specs/tool-error-middleware/tasks.md").read_text(encoding="utf-8")
    unchecked = re.findall(r"^- \[ \] \*\*(\d+\.\d+)", tasks, re.MULTILINE)
    check(unchecked == ["0.1", "7.4"], "only archive-blocked git tasks remain unchecked")
    print(f"TOOL MIDDLEWARE HANDOFF FACT-CHECK | fails={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
