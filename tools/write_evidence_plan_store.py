#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task 5.2 — write the runtime evidence pack for plan-store-and-digest.

`release_readiness.runtime_evaluation` is pending and this spec does not change
that: these are UNIT tests of one module, not EVAL cases. What they do produce is
timestamped, reproducible proof that the module behaves as the contract requires,
which is what `implementation_migration.evidence` asks for.

Writes tests/evidence/plan-store-and-digest/EVIDENCE.json plus the raw test logs.
The recorded digests become the cross-language reference: any reimplementation
(TypeScript UI, a second solver) must reproduce them or it is not equivalent.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

def _find_root() -> Path:
    """Locate the repo root by searching upward for contract/.

    Not `Path(__file__).parents[N]`: this exact off-by-one was already made once
    in tests/_fixtures.py and is being made again here, which is the signal that
    counting levels is the wrong pattern. Searching for a known marker cannot
    drift when a file moves.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "contract").is_dir() and (parent / "src" / "planpilot").is_dir():
            return parent
    raise SystemExit(
        f"repo root not found above {Path(__file__).resolve()}; "
        "expected a directory containing both contract/ and src/planpilot/"
    )


ROOT = _find_root()
OUT_DIR = ROOT / "tests" / "evidence" / "plan-store-and-digest"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

CONTRACT_PATH = ROOT / "contract" / "planpilot_agent_contract_v1.8.json"


def _run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    return proc.returncode, proc.stdout + proc.stderr


# NEGATIVE CONTROL (store) | caught=27 escaped=0 broken_fixtures=0 of 28
_NEGCTL_RE = re.compile(
    r"NEGATIVE CONTROL \(store\) \| caught=(\d+) escaped=(\d+) broken_fixtures=(\d+) of (\d+)"
)
_RESTORED_RE = re.compile(r"sources restored to pre-test bytes: (\w+)")
_WORKING_TREE_RE = re.compile(r"working repository unchanged by mutations: (\w+)")


def _extract_negative_control(out: str) -> dict:
    """Pull the mutation counters out of a negative-control run.

    Returns a dict that is merged into results["negative_control"]. `ok` is False
    when the counter line is missing, when anything escaped, when a mutation
    fixture failed to apply, or when the sources were not restored — any of which
    means the negative control did NOT do its job. A silently-missing counter is
    worse than a visible failure, because it lets an evidence pack claim green
    while carrying no mutation evidence, so it is reported as ok=False and
    all_green consults it rather than only the pytest exit code.
    """
    m = _NEGCTL_RE.search(out)
    if m is None:
        return {
            "ok": False,
            "caught": None, "escaped": None, "broken_fixtures": None, "total": None,
            "sources_restored": None,
            "detail": "counter line not found in negative-control output",
        }
    caught, escaped, broken, total = (int(g) for g in m.groups())

    r = _RESTORED_RE.search(out)
    restored = (r.group(1) == "True") if r else None
    w = _WORKING_TREE_RE.search(out)
    working_tree_unchanged = (w.group(1) == "True") if w else None

    ok = (
        escaped == 0
        and broken == 0
        and restored is True
        and working_tree_unchanged is True
        and total > 0
    )
    return {
        "ok": ok,
        "caught": caught,
        "escaped": escaped,
        "broken_fixtures": broken,
        "total": total,
        "sources_restored": restored,
        "working_repository_unchanged": working_tree_unchanged,
        "detail": m.group(0),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    contract_bytes = CONTRACT_PATH.read_bytes()

    from planpilot import CONTRACT_SHA256, CONTRACT_SCHEMA_VERSION
    from planpilot.store.digest import (
        OPERATION_SORT_KEY_FIELDS,
        canonical_json,
        canonical_plan_digest,
        sort_operations,
    )
    import _fixtures as F

    actual_sha = hashlib.sha256(contract_bytes).hexdigest()
    if actual_sha != CONTRACT_SHA256:
        raise SystemExit(f"contract drift: {actual_sha} != {CONTRACT_SHA256}")

    # ---- reference digests ------------------------------------------------
    # These are the values any reimplementation must reproduce.
    refs: dict[str, dict] = {}

    base_draft = F.make_content()
    base = F.make_content(digest=canonical_plan_digest(base_draft))
    refs["baseline_plan_content"] = {
        "digest": canonical_plan_digest(base),
        "canonical_json_sha256": hashlib.sha256(
            canonical_json({k: v for k, v in base.items() if k != "plan_digest"}).encode("utf-8")
        ).hexdigest(),
        "operations": len(base["operations"]),
        "profile": base["profile"],
    }

    # ordering invariance: shuffled input must give the same digest
    import copy, random
    shuffled_digests = set()
    for seed in range(10):
        c = copy.deepcopy(base)
        random.Random(seed).shuffle(c["operations"])
        shuffled_digests.add(canonical_plan_digest(c))
    refs["shuffled_operations_10_seeds"] = {
        "distinct_digests": len(shuffled_digests),
        "digest": sorted(shuffled_digests)[0] if len(shuffled_digests) == 1 else None,
        "matches_baseline": shuffled_digests == {canonical_plan_digest(base)},
    }

    # non-ASCII: pins ensure_ascii=False and UTF-8 encoding
    uni = F.make_content(assumptions=["假设：交期以新加坡时间为准"])
    uni_full = F.make_content(digest=canonical_plan_digest(uni),
                              assumptions=["假设：交期以新加坡时间为准"])
    refs["non_ascii_assumption"] = {
        "digest": canonical_plan_digest(uni_full),
        "canonical_text_contains_raw_cjk": "假设" in canonical_json(uni_full),
        "utf8_byte_len": len(canonical_json(uni_full).encode("utf-8")),
    }

    # int vs float distinction
    refs["int_vs_float"] = {
        "int_1": hashlib.sha256(canonical_json({"x": 1}).encode()).hexdigest(),
        "float_1_0": hashlib.sha256(canonical_json({"x": 1.0}).encode()).hexdigest(),
        "distinct": canonical_json({"x": 1}) != canonical_json({"x": 1.0}),
    }

    # signed zero has one canonical representation; solver output must not gain
    # a different digest merely because an IEEE-754 zero carries a sign bit.
    signed_negative = {"kpis": {"overtime_hours": -0.0}, "operations": []}
    signed_positive = {"kpis": {"overtime_hours": 0.0}, "operations": []}
    refs["signed_zero"] = {
        "negative_canonical_json": canonical_json(signed_negative),
        "positive_canonical_json": canonical_json(signed_positive),
        "canonical_json_equal": canonical_json(signed_negative) == canonical_json(signed_positive),
        "digest_equal": canonical_plan_digest(signed_negative) == canonical_plan_digest(signed_positive),
    }

    # An isolated surrogate cannot be UTF-8 encoded. Record that the public API
    # converts it to the registered error shape instead of leaking a codec error.
    from planpilot.store.errors import CanonicalizationError
    try:
        canonical_plan_digest({"plan_id": "PLN-SURROGATE", "note": "bad\ud800text"})
    except CanonicalizationError as exc:
        refs["isolated_surrogate"] = {
            "exception": type(exc).__name__,
            "error_code": exc.code,
            "json_path": exc.json_path,
        }
    else:  # pragma: no cover - evidence generation must fail loudly if this regresses
        raise AssertionError("isolated surrogate unexpectedly produced a digest")

    # sort key demonstration
    ops = [
        F.make_operation(start_time=F.T2, machine_id="MC-09", order_id="ORD-Z"),
        F.make_operation(start_time=F.T0, machine_id="MC-01", order_id="ORD-A"),
        F.make_operation(start_time=F.T0, machine_id="MC-09", order_id="ORD-Z"),
    ]
    refs["operation_sort_key"] = {
        "key_fields": list(OPERATION_SORT_KEY_FIELDS),
        "input_order": [(o["start_time"], o["machine_id"], o["order_id"]) for o in ops],
        "sorted_order": [(o["start_time"], o["machine_id"], o["order_id"]) for o in sort_operations(ops)],
    }

    # ---- run the suites ---------------------------------------------------
    py = sys.executable
    results = {}

    rc, out = _run([py, "-m", "pytest", "tests/unit", "-q", "--no-header", "-p", "no:cacheprovider"])
    results["unit_tests"] = {"exit_code": rc, "summary": out.strip().splitlines()[-1] if out.strip() else ""}
    (OUT_DIR / "unit_tests.log").write_bytes(out.replace("\r\n", "\n").encode("utf-8"))

    rc2, out2 = _run([py, "-m", "pytest", "tests/negative_control", "-q", "--no-header", "-s",
                      "-p", "no:cacheprovider"])
    # The LAST line of a `-s` negative-control run is pytest's "N passed", which
    # says nothing useful: those 3 tests are wrappers around 28 mutations. The
    # numbers that matter are printed mid-output by the test itself. Recording
    # only the last line is how an evidence pack ends up claiming green while
    # carrying no mutation results at all, so the counter line is extracted
    # explicitly — and a missing or non-zero-escape line is treated as a FAILURE,
    # not as a cosmetic gap.
    negctl = _extract_negative_control(out2)
    results["negative_control"] = {
        "exit_code": rc2,
        "summary": out2.strip().splitlines()[-1] if out2.strip() else "",
        **negctl,
    }
    (OUT_DIR / "negative_control.log").write_bytes(out2.replace("\r\n", "\n").encode("utf-8"))

    rc3, out3 = _run([py, str(ROOT / "tools" / "check_closed_vocabularies.py"), "--self-test"])
    results["vocabulary_guard_self_test"] = {"exit_code": rc3, "summary": out3.strip().splitlines()[-1] if out3.strip() else ""}

    rc4, out4 = _run([py, str(ROOT / "tools" / "check_closed_vocabularies.py")])
    results["vocabulary_guard_repo_scan"] = {"exit_code": rc4, "summary": out4.strip().splitlines()[-1] if out4.strip() else ""}
    (OUT_DIR / "vocabulary_guard.log").write_bytes(out4.replace("\r\n", "\n").encode("utf-8"))

    rc5, out5 = _run([py, str(ROOT / "tools" / "validate_kiro_workspace.py")])
    results["workspace_validation"] = {"exit_code": rc5, "summary": out5.strip().splitlines()[-1] if out5.strip() else ""}

    rc6, out6 = _run([py, str(ROOT / "tools" / "negative_control_workspace.py")])
    results["workspace_negative_control"] = {"exit_code": rc6, "summary": out6.strip().splitlines()[-1] if out6.strip() else ""}

    # ---- versions ---------------------------------------------------------
    from importlib.metadata import version as _pkg_version

    def _ver(pkg: str) -> str:
        try:
            return _pkg_version(pkg)
        except Exception:
            return "unknown"

    versions = {
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        # importlib.metadata, not module.__version__: the latter is deprecated
        # for jsonschema and would emit a warning into the evidence log.
        "jsonschema": _ver("jsonschema"),
    }
    try:
        import ortools  # noqa: F401
        versions["ortools"] = _ver("ortools")
    except ImportError:
        versions["ortools"] = "not installed (this spec does not use the solver)"
    versions["pytest"] = _ver("pytest")

    evidence = {
        "spec": "plan-store-and-digest",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "contract": {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "sha256": actual_sha,
            "bytes": len(contract_bytes),
        },
        "scope_statement": (
            "UNIT evidence for one module (src/planpilot/store). This is NOT an EVAL "
            "result and does NOT make the contract pilot-ready: "
            "release_readiness.runtime_evaluation remains pending and no EVAL case "
            "(EVAL-001..030) has been executed. No LLM, network, credential or "
            "dataset was involved."
        ),
        "versions": versions,
        "reference_digests": refs,
        "results": results,
        # exit_code alone is not enough: the negative control prints its mutation
        # counters mid-output and can exit 0 while carrying no usable numbers, so
        # its own `ok` flag is part of green. Every other entry has no `ok` key
        # and falls back to exit_code.
        "all_green": all(
            r.get("exit_code") == 0 and r.get("ok", True)
            for r in results.values()
        ),
        "known_findings": [
            {
                "id": "F-STORE-01",
                "summary": "No registered error code describes an illegal lifecycle transition.",
                "detail": (
                    "VALIDATION_FAILED's details schema is the factory-state shape "
                    "(status/errors/quarantined_entity_count); POLICY_VIOLATION's "
                    "violated_policy is a closed enum with no fitting value; "
                    "PLAN_VERSION_CONFLICT means 'you expected a different version'. "
                    "TransitionNotAllowedError and LifecycleAlreadyExistsError are "
                    "therefore StoreInvariantError with NO contract code: caller "
                    "programming errors that must never be rendered as a tool_error. "
                    "If a runtime path ever needs to refuse a transition AND tell the "
                    "user, the contract needs a new registered code."
                ),
                "status": "open — needs a contract decision, not a code fix",
            },
            {
                "id": "F-STORE-02",
                "summary": "The two non-finite-float layers are not interchangeable.",
                "detail": (
                    "_reject_non_finite raises CanonicalizationError with "
                    "contract-shaped details and a json_path; allow_nan=False raises a "
                    "bare ValueError with neither. Removing layer 1 still blocks NaN "
                    "but produces an error the tool layer cannot turn into a valid "
                    "tool_error. Measured by the negative control, which initially "
                    "assumed the layers were redundant and was wrong."
                ),
                "status": "resolved — pinned by test_layer1_error_is_not_the_same_as_layer2",
            },
            {
                "id": "F-STORE-03",
                "summary": "Canonicalization leaked UnicodeEncodeError and distinguished signed zero.",
                "detail": (
                    "Isolated UTF-16 surrogates in string values or object keys are "
                    "now rejected as contract-shaped CanonicalizationError before "
                    "UTF-8 encoding. Every -0.0 value is normalized on a copied tree "
                    "to 0.0, so semantically equal signed zeros have one digest without "
                    "mutating caller input."
                ),
                "status": "resolved — pinned by TestUnicodeAndSignedZeroEdges",
            },
        ],
    }

    text = json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    (OUT_DIR / "EVIDENCE.json").write_bytes(text.replace("\r\n", "\n").encode("utf-8"))

    print(f"evidence written to {OUT_DIR.relative_to(ROOT)}")
    print(f"  contract sha256: {actual_sha[:16]}…")
    print(f"  baseline digest: {refs['baseline_plan_content']['digest'][:32]}…")
    print(f"  all_green: {evidence['all_green']}")
    for name, r in results.items():
        mark = "OK " if (r["exit_code"] == 0 and r.get("ok", True)) else "FAIL"
        # Prefer the negative control's own counter line over pytest's "N passed",
        # which hides the mutation numbers that are the entire point of that run.
        shown = r.get("detail") or r["summary"]
        print(f"  {mark} {name}: {shown[:78]}")
    return 0 if evidence["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
