#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fact-check REVIEW_HANDOFF_IMPLEMENTATION.md against reality.

This document goes to an external reviewer. A wrong number in it is worse than no
number, and this project has already shipped a commit message claiming an artefact
that did not exist. So every claim is re-derived here.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

def _find_root() -> Path:
    """Search upward for the repo markers instead of counting parent levels.

    This file's first draft used `Path(__file__).parent`, which resolved to
    tools/ rather than the repo root. That is the same off-by-one recorded as
    defect D9 in IMPLEMENTATION_NOTES.md — already made twice, now three times.
    Counting levels is the wrong pattern; searching for a known marker cannot
    drift when a file moves.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "contract").is_dir() and (parent / "src" / "planpilot").is_dir():
            return parent
    raise SystemExit(f"repo root not found above {Path(__file__).resolve()}")

ROOT = _find_root()
DOC = ROOT / "REVIEW_HANDOFF_IMPLEMENTATION.md"
text = DOC.read_text(encoding="utf-8")

fails = []
def ck(cond, label):
    print(f"  {'OK  ' if cond else 'FAIL'} | {label}")
    if not cond:
        fails.append(label)


def run(args, cwd=ROOT):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    return p.returncode, p.stdout + p.stderr


PY = r"E:\PlanPilot-Hackathon\contract-review\.venv\Scripts\python.exe"

print("=== A. git claims ===")
rc, out = run(["git", "log", "--oneline"])
commits = [l for l in out.strip().splitlines() if l.strip()]
ck(len(commits) == 6, f"6 commits (actual {len(commits)})")
ck("98ee9a8" in text and commits[0].startswith("98ee9a8"), f"HEAD is 98ee9a8 (actual {commits[0][:7]})")
rc, out = run(["git", "status", "--porcelain", "src/planpilot/store"])
# Scope is src/planpilot/store on purpose, NOT the whole repo. The point of this
# check is that the negative control restores every mutation it injects into the
# store sources. Checking the whole tree instead makes the factcheck fail on any
# unrelated in-progress edit (a doc, a tool), which is noise, not signal.
extra = [l for l in out.strip().splitlines() if l.strip()]
ck(not extra, f"store sources clean — negative control restored them (dirty: {extra})")
rc, out = run(["git", "remote", "-v"])
ck(out.strip() == "" and "no remote yet" in text, "no remote configured")
rc, out = run(["git", "ls-files"])
tracked = [l for l in out.strip().splitlines() if l.strip()]
ck(f"{len(tracked)} files tracked in git" in text,
   f"doc states the tracked file count {len(tracked)}")

print("\n=== B. line-count claims (table parsed from the doc, recomputed from disk) ===")
# Parse the markdown table rows rather than hardcoding labels: a checker that
# breaks when the table gains a column is worse than no checker.
def _lines(rel: str) -> int:
    return len((ROOT / rel).read_text(encoding="utf-8").splitlines())

# Map each row label to the files it denotes, derived from the label's own path
# text. The labels in the table name real paths, so resolve them against disk.
def _files_for_label(label: str) -> list[str]:
    lab = label.replace("`", "")
    if lab.startswith("src/planpilot/"):
        return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "src").rglob("*.py"))
    if lab.startswith("tests/unit/"):
        return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests/unit").rglob("*.py"))
    if lab.startswith("tests/negative_control/"):
        return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests/negative_control").rglob("*.py"))
    if "_fixtures.py" in lab:
        return [f for f in ("tests/_fixtures.py", "tests/conftest.py", "tests/__init__.py")
                if (ROOT / f).exists()]
    # single-file rows: the label IS the path
    cand = lab.strip()
    return [cand] if (ROOT / cand).exists() else []

rows = re.findall(r"^\| (.+?) \| \*{0,2}([\d,]+)\*{0,2} \| \*{0,2}(\d+)\*{0,2} \|$", text, re.M)
ck(bool(rows), "found the line-count table in the doc")
sum_lines = sum_files = 0
doc_total_lines = doc_total_files = None
for label, ln, fl in rows:
    ln_i, fl_i = int(ln.replace(",", "")), int(fl)
    # The total row is labelled "**subject total**" — it excludes this factcheck
    # tool, which measures the table and would otherwise make the figure move on
    # every edit to itself (the same circularity canonical_plan_digest avoids by
    # excluding plan_digest from the hashed payload).
    if "total" in label.strip().lower():
        doc_total_lines, doc_total_files = ln_i, fl_i
        continue
    files = _files_for_label(label)
    # this tool is deliberately not a table row; if it ever reappears, skip it
    files = [f for f in files if not f.endswith("factcheck_impl_handoff.py")]
    actual_lines = sum(_lines(f) for f in files)
    ck(actual_lines == ln_i, f"{label}: doc {ln_i} lines, actual {actual_lines} ({len(files)} files)")
    ck(len(files) == fl_i, f"{label}: doc {fl_i} files, actual {len(files)}")
    sum_lines += actual_lines
    sum_files += len(files)

# the table's own total row must equal the sum of its parts
ck(doc_total_lines == sum_lines, f"total row {doc_total_lines} == sum of rows {sum_lines}")
ck(doc_total_files == sum_files, f"total row {doc_total_files} files == sum {sum_files}")

# the prose 'Scale' sentence must agree with the table's subject total
m = re.search(r"Scale: \*\*([\d,]+) lines\*\* across (\d+) Python files", text)
ck(m is not None, "Scale sentence is present and well-formed")
if m:
    ck(int(m.group(1).replace(",", "")) == doc_total_lines, f"Scale lines {m.group(1)} == subject total {doc_total_lines}")
    ck(int(m.group(2)) == doc_total_files, f"Scale files {m.group(2)} == subject total {doc_total_files}")
mt = re.search(r"(\d+) files tracked in git", text)
ck(mt is not None and int(mt.group(1)) == len(tracked),
   f"Scale tracked {mt.group(1) if mt else '?'} == git ls-files {len(tracked)}")

print("\n=== C. test-count claims (parsed from the doc, compared to a live run) ===")
# Nothing here is hardcoded: each claimed number is read out of the doc's
# verification table and compared against what pytest actually reports. Adding a
# test therefore cannot make this section stale — it makes the doc wrong, which
# is exactly what this file is for.

def _claimed_passed(label_fragment: str) -> int | None:
    """The **N passed** value in the verification-table row mentioning a fragment."""
    row = re.search(r"\|[^\n]*" + re.escape(label_fragment) + r"[^\n]*\*\*(\d+) passed\*\*[^\n]*\|", text)
    return int(row.group(1)) if row else None

# unit row
rc, out = run([PY, "-m", "pytest", "tests/unit", "-q", "--no-header", "-p", "no:cacheprovider"])
unit_actual = int(re.search(r"(\d+) passed", out).group(1))
unit_claimed = _claimed_passed("pytest tests/unit")
ck(unit_claimed is not None, "doc states a unit-test pass count")
ck(unit_claimed == unit_actual, f"unit tests: doc claims {unit_claimed}, pytest reports {unit_actual}")

# negative-control row + the mutation tally in its body
rc, out = run([PY, "-m", "pytest", "tests/negative_control", "-q", "--no-header", "-s", "-p", "no:cacheprovider"])
neg = re.search(r"caught=(\d+) escaped=(\d+) broken_fixtures=(\d+) of (\d+)", out)
ck(neg is not None, "negative control reports its tally")
if neg:
    caught, escaped, broken, total = (int(g) for g in neg.groups())
    ck(escaped == 0 and broken == 0, f"negative control: 0 escaped / 0 broken (actual {escaped}/{broken})")
    ck(caught == total - 1, f"caught {caught} == total {total} minus the one defence-in-depth case")
    for token in (f"{caught} caught", "0 escaped", "0 broken", f"of {total}"):
        ck(token in text, f"doc states {token!r}")
nc_actual = int(re.search(r"(\d+) passed", out).group(1))
nc_claimed = _claimed_passed("pytest tests/negative_control")
ck(nc_claimed == nc_actual, f"negative-control tests: doc claims {nc_claimed}, pytest reports {nc_actual}")
ck("source tree restored" in out.lower() or "restored: True" in out, "sources restored after mutations")

# full suite: the doc cites a historical 152 (clean-env) and a current total
rc, out = run([PY, "-m", "pytest", "tests/", "-q", "--no-header", "-p", "no:cacheprovider"])
full_actual = int(re.search(r"(\d+) passed", out).group(1))
ck(full_actual == unit_actual + nc_actual, f"full {full_actual} == unit {unit_actual} + negctl {nc_actual}")
m = re.search(r"now \*\*(\d+) tests\*\*", text)
ck(m is not None, "doc states the current full-suite size")
if m:
    ck(int(m.group(1)) == full_actual, f"doc's current total {m.group(1)} == pytest {full_actual}")
ck("152 passed in 27.71s" in text, "doc keeps the historical clean-env result, labelled as historical")

print("\n=== D. guard claims ===")
rc, out = run([PY, str(ROOT / "tools/check_closed_vocabularies.py"), "--self-test"])
oks = len(re.findall(r"^  OK   \|", out, re.M))
ck(rc == 0 and "SELF-TEST | PASS" in out, "guard self-test passes")
ck(oks == 17 and "17 cases" in text, f"self-test has 17 cases (actual {oks})")

rc, out = run([PY, str(ROOT / "tools/check_closed_vocabularies.py")])
ck(rc == 0 and "CLOSED VOCABULARY CHECK | PASS" in out, "guard repo scan passes")
m = re.search(r"vocabularies: (\d+) sets, (\d+) known members", out)
ck(m is not None, "guard reports vocabulary counts")
if m:
    ck(f"{m.group(1)} vocabularies" in text and f"{m.group(2)} members" in text,
       f"doc states {m.group(1)} vocabularies / {m.group(2)} members (actual {m.group(1)}/{m.group(2)})")
m = re.search(r"scanning (\d+) modules", out)
if m:
    ck(f"{m.group(1)} modules" in text, f"doc states {m.group(1)} modules scanned")
# allowlist size
sys.path.insert(0, str(ROOT / "tools"))
import importlib.util
spec = importlib.util.spec_from_file_location("g", ROOT / "tools/check_closed_vocabularies.py")
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
ck(f"now {len(g.ALLOWED_LOCAL)} entries" in text,
   f"doc states the allowlist size (actual {len(g.ALLOWED_LOCAL)})")

rc, out = run([PY, str(ROOT / "tools/validate_kiro_workspace.py")])
ck(rc == 0 and "ok=190 fail=0" in out and "**ok=190 fail=0**" in text, "workspace validation 190/0")
rc, out = run([PY, str(ROOT / "tools/negative_control_workspace.py")])
ck(rc == 0 and "caught=14 escaped=0 of 14" in out, "workspace negative control 14/14")

print("\n=== E. contract hash unchanged ===")
h = hashlib.sha256((ROOT / "contract/planpilot_agent_contract_v1.8.json").read_bytes()).hexdigest()
ck(h == "b92e53f4ff0541050ec6585f3237f4d764a26e455447a672f3419dfb284fe639", f"contract sha256 {h[:16]}…")
ck("b92e53f4ff054105" in text, "doc quotes the contract hash prefix")

print("\n=== F. referenced files all exist ===")
for rel in ["IMPLEMENTATION_NOTES.md",
            ".kiro/specs/plan-store-and-digest/design.md",
            ".kiro/specs/plan-store-and-digest/tasks.md",
            "src/planpilot/store/digest.py", "src/planpilot/store/errors.py",
            "src/planpilot/store/plan_store.py",
            "tools/check_closed_vocabularies.py",
            "tests/negative_control/test_plan_store_negctl.py",
            "tests/evidence/plan-store-and-digest/EVIDENCE.json",
            "requirements-dev.txt", ".kiro/hooks/guard-spec-tasks.json",
            "../contract-review/REVIEW_HANDOFF_FOR_CODEX.md"]:
    ck((ROOT / rel).exists(), f"{rel} exists")

print("\n=== G. named tests actually exist ===")
named = re.findall(r"`(test_[a-z0-9_]+)`", text)
# Gather the actual test sources from disk rather than a hardcoded list, so this
# check cannot go stale when a test module is added.
src = "\n".join(
    p.read_text(encoding="utf-8")
    for p in sorted((ROOT / "tests").rglob("test_*.py"))
)
missing = sorted({n for n in named if f"def {n}" not in src})
ck(not missing, f"every test named in the doc exists (missing: {missing})")

print("\n=== H. defect and finding ids are all documented in the notes ===")
notes = (ROOT / "IMPLEMENTATION_NOTES.md").read_text(encoding="utf-8")
for did in [f"D{i}" for i in range(1, 14)] + ["F-STORE-01", "F-STORE-02"]:
    ck(did in text, f"{did} mentioned in the handoff")
    ck(did in notes, f"{did} documented in IMPLEMENTATION_NOTES.md")
ck("13 defects" in text or "all 13" in text, "doc states the defect count as 13")
m = re.search(r"## 8\. What I got wrong \(all (\d+)", text)
ck(m and int(m.group(1)) == 13, f"section 8 heading says 13 (actual {m.group(1) if m else '?'})")
# the D5 row is bolded (**D5**), so the cell may carry markdown emphasis
rows = re.findall(r"^\| \*{0,2}(D\d+)\*{0,2} \|", text, re.M)
ck(len(rows) == 13, f"section 8 table has 13 rows (actual {len(rows)}): {rows}")
ck(rows == [f"D{i}" for i in range(1, 14)], f"rows are D1..D13 in order: {rows}")

print("\n=== I. honesty claims ===")
ck("no such file existed" in text, "doc admits the REVIEW-notes claim preceded the artefact")
ck("understated the count as \"four\"" in text, "doc admits the 'four' understatement")
ck("not an EVAL result" in text.lower() or "No EVAL case has been executed" in text,
   "doc states plainly that no EVAL has run")
ck("pilot-ready" in text, "doc addresses pilot readiness")
ck("in-memory" in text, "doc discloses the store is in-memory")
ck("not checked" in text.lower(), "doc admits at least one unchecked item")

print("\n=== J. evidence pack contents match the doc ===")
ev = json.loads((ROOT / "tests/evidence/plan-store-and-digest/EVIDENCE.json").read_bytes().decode("utf-8"))
ck(ev["all_green"] is True, "evidence pack reports all_green")
ck(ev["contract"]["sha256"] == h, "evidence pack contract hash matches disk")
rd = ev["reference_digests"]
ck(rd["shuffled_operations_10_seeds"]["matches_baseline"] is True, "shuffle invariance recorded as true")
ck(rd["int_vs_float"]["distinct"] is True, "int/float distinction recorded as true")
ck(rd["non_ascii_assumption"]["canonical_text_contains_raw_cjk"] is True, "non-ASCII recorded as raw CJK")
ck("scope_statement" in ev and "NOT an EVAL" in ev["scope_statement"], "scope statement disclaims EVAL status")
ck("reference digests" in text.lower(), "doc mentions the reference digests")

print(f"\nHANDOFF FACT-CHECK | fails={len(fails)}")
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
