#!/usr/bin/env python3
"""Check P1-2 report against its captured evidence and current source bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tests/evidence/p1-2-scheduling-engine/EVIDENCE.json"
REPORT = ROOT / "P1_2_SCHEDULING_ENGINE_REMEDIATION_REPORT_20260919.md"


def main() -> int:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")
    failures = []

    def check(ok: bool, label: str) -> None:
        print(f"{'OK' if ok else 'FAIL'} | {label}")
        if not ok:
            failures.append(label)

    check(evidence["all_green_offline"] is True, "offline evidence green")
    check(hashlib.sha256((ROOT / "contract/planpilot_agent_contract_v1.8.json").read_bytes()).hexdigest()
          == evidence["contract_sha256"], "contract digest unchanged")
    for path, expected in evidence["subject_sha256"].items():
        check(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected,
              f"subject digest {path}")
    for name in ("focused", "unit", "full"):
        count = evidence["results"][name]["passed"]
        check(count is not None and f"{count} passed" in report, f"{name} count in report")
    for name, result in evidence["results"].items():
        log = (EVIDENCE.parent / f"{name}.log").read_bytes()
        check(hashlib.sha256(log).hexdigest() == result["log_sha256"], f"raw {name} log")
    check(evidence["formal_eval"] == {"passed": 0, "failed": 0, "blocked": 30}
          and "0 PASS / 0 FAIL / 30 BLOCKED" in report, "formal EVAL remains blocked")
    tasks = (ROOT / ".kiro/specs/p1-2-scheduling-engine/tasks.md").read_text(encoding="utf-8")
    check(tasks.count("- [ ]") == 5, "five open implementation/deployment tasks declared")
    print(f"P1-2 HANDOFF FACT-CHECK | fails={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
