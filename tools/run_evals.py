"""Fail-closed inventory of formal contract evaluations, not a smoke runner.

No case-specific end-to-end executor/evidence verifier is implemented yet.
Installing a workbook or supplying an old PASS artifact cannot unlock a case.
Future executors must verify the entire contract pass_condition and retain
reconstructable inputs, outputs, traces, digest, timing, approvals and audit.
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
MISSING_EVIDENCE = (
    "case_specific_executor_and_full_pass_condition_verifier",
    "complete_case_inputs_and_pinned_runtime_configuration",
    "complete_case_outputs_and_independent_validation",
    "reconstructable_decision_trace_per_tool_invocation",
    "recomputed_canonical_plan_digest_or_verified_no_plan_outcome",
    "measured_stage_and_end_to_end_timings",
    "bound_approval_decisions_or_case_specific_non_applicability_proof",
    "verified_audit_hash_chain_and_failure_atomicity",
)


def run(output: Path = OUT):
    raw = CONTRACT.read_bytes()
    contract = json.loads(raw)
    cases = contract["acceptance_tests"]
    expected = {f"EVAL-{number:03d}" for number in range(1, 31)}
    if len(cases) != len(expected) or {c["case_id"] for c in cases} != expected:
        raise ValueError("Incomplete or duplicate contract acceptance case inventory")
    results = [
        {**case, "status": "BLOCKED", "executed": False,
         "reason": "Case-specific formal executor and complete verified evidence are not implemented.",
         "missing_evidence": list(MISSING_EVIDENCE)}
        for case in cases
    ]
    evidence = {
        "evidence_version": 2,
        "evidence_kind": "formal_eval_readiness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_sha256": hashlib.sha256(raw).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version, "platform": platform.platform(),
        "case_count": len(results), "passed": 0, "failed": 0,
        "blocked": len(results), "results": results,
        "runtime_evaluation": contract["release_readiness"]["runtime_evaluation"],
        "scope_statement": "Readiness inventory only. No formal case executed; smoke/unit results do not establish formal acceptance.",
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / "EVIDENCE.json"
    temporary = output / "EVIDENCE.json.tmp"
    temporary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args(argv)
    result = run(args.output)
    print(f"formal EVAL readiness evidence: {args.output}")
    print(f"cases={result['case_count']} passed={result['passed']} failed={result['failed']} blocked={result['blocked']}")
    return 0 if result["case_count"] > 0 and result["passed"] == result["case_count"] and not (result["failed"] or result["blocked"]) else 1


if __name__ == "__main__":
    sys.exit(main())
