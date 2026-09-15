"""Write fail-closed evidence for the contract acceptance suite.

This command is the formal acceptance gate. A case can become PASS only after
it has a dedicated end-to-end runner that proves the exact ``pass_condition``
and records reviewable evidence references. Component checks belong in
``run_smoke_harness.py`` and can never promote an EVAL case.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contract" / "planpilot_agent_contract_v1.8.json"
OUT = ROOT / "tests" / "evidence" / "runtime-eval"
EXPECTED_CASE_IDS = tuple(f"EVAL-{index:03d}" for index in range(1, 31))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contract_cases() -> list[dict]:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    cases = contract.get("acceptance_tests")
    if not isinstance(cases, list):
        raise RuntimeError("contract acceptance_tests is not a list")
    ids = tuple(case.get("case_id") for case in cases)
    if ids != EXPECTED_CASE_IDS or len(set(ids)) != len(ids):
        raise RuntimeError("contract acceptance case inventory is not exactly EVAL-001..030")
    required = {"case_id", "test_type", "scenario", "pass_condition"}
    if any(not required.issubset(case) for case in cases):
        raise RuntimeError("contract acceptance case is incomplete")
    return cases


def build_evidence() -> dict:
    """Build an honest snapshot; no formal case runner exists in this part."""
    results = []
    for case in _contract_cases():
        results.append(
            {
                "case_id": case["case_id"],
                "test_type": case["test_type"],
                "scenario": case["scenario"],
                "pass_condition": case["pass_condition"],
                "status": "BLOCKED",
                "reason": (
                    "No dedicated end-to-end case runner currently executes and proves "
                    "this exact contract pass condition. Component smoke checks are not "
                    "acceptance evidence."
                ),
                "unmet_pass_condition": case["pass_condition"],
                "evidence_refs": [],
            }
        )
    return {
        "evidence_version": 2,
        "evidence_kind": "formal_acceptance_gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_sha256": _sha256(CONTRACT),
        "harness_sha256": _sha256(Path(__file__)),
        "python": sys.version,
        "platform": platform.platform(),
        "case_count": len(results),
        "passed": 0,
        "failed": 0,
        "blocked": len(results),
        "acceptance_ready": False,
        "acceptance_claimed": False,
        "runtime_evaluation": "PENDING_UNTIL_EVAL_001_TO_030_EXECUTE",
        "blocking_findings": [
            {
                "id": "formal_case_runners_missing",
                "detail": "No case-specific end-to-end runners with reviewable artifacts are registered.",
            },
            {
                "id": "public_tool_boundary_incomplete",
                "detail": "The contract-shaped public tool/error middleware boundary is not implemented.",
            },
            {
                "id": "runtime_integration_incomplete",
                "detail": "The demo runtime is not yet fully integrated with the audited store and approval services.",
            },
        ],
        "scope_statement": (
            "This is a fail-closed readiness snapshot, not proof that an EVAL case passed. "
            "Formal PASS requires execution of the exact scenario and pass condition with "
            "timestamped, independently inspectable artifacts."
        ),
        "results": results,
    }


def write_evidence(output: Path = OUT) -> dict:
    evidence = build_evidence()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "EVIDENCE.json"
    temporary = output / "EVIDENCE.json.tmp"
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args(argv)
    result = write_evidence(args.output)
    print(f"formal acceptance evidence: {args.output}")
    print(
        f"cases={result['case_count']} passed={result['passed']} "
        f"failed={result['failed']} blocked={result['blocked']}"
    )
    print("NOT READY: formal acceptance remains blocked; component smoke checks do not count.")
    return 0 if result["acceptance_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
