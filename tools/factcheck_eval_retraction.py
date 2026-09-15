"""Read-only fact-check for the formal-EVAL retraction and smoke separation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contract" / "planpilot_agent_contract_v1.8.json"
FORMAL = ROOT / "tests" / "evidence" / "runtime-eval" / "EVIDENCE.json"
SMOKE = ROOT / "tests" / "evidence" / "smoke-harness" / "EVIDENCE.json"
CURRENT_DOCS = (
    ROOT / "START_HERE.md",
    ROOT / "PRODUCT_RUNBOOK.md",
    ROOT / "PROJECT_OVERVIEW_BILINGUAL.md",
    ROOT / "tests" / "evidence" / "runtime-eval" / "README.md",
    ROOT / "tests" / "evidence" / "smoke-harness" / "README.md",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(("PASS" if condition else "FAIL") + " | " + label)
        if not condition:
            failures.append(label)

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    formal = json.loads(FORMAL.read_text(encoding="utf-8"))
    smoke = json.loads(SMOKE.read_text(encoding="utf-8"))
    cases = contract["acceptance_tests"]
    expected_ids = [case["case_id"] for case in cases]
    formal_results = formal.get("results", [])

    check(len(cases) == 30 and len(set(expected_ids)) == 30, "contract has 30 unique acceptance cases")
    check(formal.get("contract_sha256") == _sha256(CONTRACT), "formal evidence binds the current contract")
    check(
        formal.get("harness_sha256") == _sha256(ROOT / "tools" / "run_evals.py"),
        "formal evidence binds the current formal harness",
    )
    check([row.get("case_id") for row in formal_results] == expected_ids, "formal evidence preserves case order")
    check(
        [row.get("pass_condition") for row in formal_results]
        == [case["pass_condition"] for case in cases],
        "formal evidence preserves exact pass conditions",
    )
    check(
        formal.get("passed") == 0 and formal.get("failed") == 0 and formal.get("blocked") == 30,
        "formal counts are 0 passed, 0 failed, 30 blocked",
    )
    check(
        all(row.get("status") == "BLOCKED" and row.get("evidence_refs") == [] for row in formal_results),
        "every formal case is blocked without fabricated evidence references",
    )
    check(
        formal.get("acceptance_ready") is False and formal.get("acceptance_claimed") is False,
        "formal evidence makes no acceptance claim",
    )
    check(
        formal.get("runtime_evaluation") == "PENDING_UNTIL_EVAL_001_TO_030_EXECUTE",
        "formal runtime status remains pending",
    )

    smoke_results = smoke.get("results", [])
    check(smoke.get("evidence_kind") == "component_smoke", "smoke evidence is explicitly component-scoped")
    check(smoke.get("contract_sha256") == _sha256(CONTRACT), "smoke evidence binds the current contract")
    check(
        smoke.get("harness_sha256") == _sha256(ROOT / "tools" / "run_smoke_harness.py"),
        "smoke evidence binds the current smoke harness",
    )
    check(smoke.get("acceptance_claimed") is False, "smoke evidence makes no acceptance claim")
    check(
        all("check_id" in row and "case_id" not in row and row.get("status") in {"pass", "fail"}
            for row in smoke_results),
        "smoke records cannot masquerade as formal case records",
    )
    check(
        smoke.get("smoke_passed") == len(smoke_results) and smoke.get("smoke_failed") == 0,
        "all recorded component smoke checks passed",
    )

    docs = "\n".join(path.read_text(encoding="utf-8") for path in CURRENT_DOCS)
    check(not re.search(r"30\s*/?\s*30\s+PASS|30\s+PASS", docs, re.IGNORECASE), "current docs contain no 30-pass claim")
    check("0 PASS / 0 FAIL / 30 BLOCKED" in docs, "current docs publish the blocked formal count")
    check("run_smoke_harness.py" in docs and "run_evals.py" in docs, "current docs distinguish both commands")

    formal_source = (ROOT / "tools" / "run_evals.py").read_text(encoding="utf-8")
    check("check = True" not in formal_source and "100 < 300" not in formal_source, "old shortcut assertions are absent")
    check("return 0 if result[\"acceptance_ready\"] else 2" in formal_source, "formal CLI fails closed")

    manifest_path = ROOT / "PACKAGE_MANIFEST.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest.get("files", [])
        paths = [entry.get("path") for entry in entries]
        valid = len(paths) == len(set(paths)) and "PACKAGE_MANIFEST.json" not in paths
        valid = valid and all(
            (ROOT / entry["path"]).is_file()
            and (ROOT / entry["path"]).stat().st_size == entry["size"]
            and _sha256(ROOT / entry["path"]) == entry["sha256"]
            for entry in entries
        )
        check(valid, "package manifest is unique and hashes every listed file")
    else:
        check(False, "package manifest exists")

    print(f"EVAL RETRACTION FACT-CHECK | fails={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
