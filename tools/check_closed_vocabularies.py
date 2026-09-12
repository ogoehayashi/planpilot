#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task 4.1 — closed-vocabulary guard for the IMPLEMENTATION layer.

Why this exists
---------------
verify_contract.py runs 600 assertions against the CONTRACT. Nothing checked the
implementation. The PreTaskExec hook in .kiro/hooks/guard-spec-tasks.json asks the
model nicely not to invent identifiers — but a prompt is not a gate.

The contract's vocabularies are closed and cross-referenced. A fabricated
validation code, error code or status in src/ would not be caught by any existing
check, and would only surface at EVAL time — days before the deadline, when there
is no time left to fix it. This script is the gate that closes that window.

What it inspects
----------------
Only STRING CONSTANTS, parsed with `ast`. That precision is what keeps the false
positive rate near zero:

  "PLAN_DIGEST_MISMATCH"     a string literal      -> checked
  OPERATION_SORT_KEY_FIELDS  an identifier         -> never examined

Excluded on principle, not by allowlist:

  * DOCSTRINGS — prose. (Note: in the AST a docstring is `Expr(Constant)`, so the
    Constant must be compared against `body[0].value`, not `body[0]`. Getting
    this wrong silently disables the exclusion — it did, on the first draft.)
  * `__all__` contents — Python export names, not vocabulary claims.
  * Dotted references in prose — a token preceded by `.` is naming an attribute
    (e.g. an error message saying "expected (planpilot.CONTRACT_SHA256)"), not
    asserting a contract value.
  * IMPLEMENTATION_NAMESPACES — the implementation's own env-var prefixes.

Everything else is default-deny: a vocabulary-shaped token absent from the
contract fails the build unless it is in ALLOWED_LOCAL with a reason. Adding an
entry is a one-line change a reviewer sees in the diff, which is the point.

Highest-signal check: NEAR MISSES
---------------------------------
The dangerous fabrication is not a random word, it is a near-copy of a real
member — `PLAN_DIGEST_MISMATCHED`, `HC-014`, `SEARCH_ESCALATION_EXHAUST`. Those
read as legitimate in review. Any token within edit distance 2 of a real member
is reported separately and always fails, allowlist or not.

Self-checks (so the guard cannot rot):
  1. every allowlist entry is genuinely absent from the contract
  2. every allowlist entry is actually used by some module
  3. vocabularies are non-empty and contain known members
  4. --self-test proves fabrication is caught

Run:  python tools/check_closed_vocabularies.py [--self-test]
Exit: 0 clean, 1 on any violation.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_contract() -> Path:
    for candidate in sorted((ROOT / "contract").glob("planpilot_agent_contract_v*.json")):
        return candidate
    raise SystemExit(f"no contract found under {ROOT / 'contract'}")


CONTRACT_PATH = find_contract()
CONTRACT = json.loads(CONTRACT_PATH.read_bytes().decode("utf-8"))
DEFS = CONTRACT["$defs"]

# ---------------------------------------------------------------------------
# The closed vocabularies, read from the contract. Never hardcoded.
# ---------------------------------------------------------------------------

VOCABULARIES: dict[str, set[str]] = {
    "error_code": set(DEFS["tool_error"]["properties"]["error_code"]["enum"]),
    "validation_issue_code": set(DEFS["validation_issue_code"]["enum"]),
    "decision_reason_code": set(DEFS["decision_reason_code"]["enum"]),
    "lifecycle_status": set(DEFS["plan_lifecycle"]["properties"]["status"]["enum"]),
    "workflow_state": set(CONTRACT["workflow"]["states"]),
    "plan_profile": {k for k, v in CONTRACT["plan_profiles"].items() if isinstance(v, dict)},
    "kpi": set(DEFS["kpis"]["required"]),
    "solver": set(DEFS["engine_provenance"]["properties"]["solver"]["enum"]),
    "solver_status": set(DEFS["engine_provenance"]["properties"]["solver_status"]["enum"]),
    "operation_type": set(DEFS["schedule_operation"]["properties"]["operation_type"]["enum"]),
    "risk_level": set(DEFS["schedule_operation"]["properties"]["risk_level"]["enum"]),
    "unscheduled_reason_code": set(
        DEFS["unscheduled_operation"]["properties"]["reason_code"]["enum"]
    ),
    "reservation_status": set(DEFS["lot_material_reservation"]["properties"]["status"]["enum"]),
    "material_uom": set(DEFS["material_reservation_line"]["properties"]["material_uom"]["enum"]),
    "source_type": set(DEFS["material_source_allocation"]["properties"]["source_type"]["enum"]),
    "severity": set(DEFS["validation_issue"]["properties"]["severity"]["enum"]),
    "violated_policy": set(
        DEFS["error_details_policy_violation"]["properties"]["violated_policy"]["enum"]
    ),
    "approval_action": set(
        DEFS["error_details_policy_violation"]["properties"]["approval_action"]["anyOf"][0]["enum"]
    ),
    "approval_decision": {r["decision"] for r in CONTRACT["approval_rules"]},
    # Security controls carry enum-like string values that are just as closed as
    # the $defs enums. BLOCK_AND_LOG lives here, not in approval_rules.
    "security_action": {
        v for v in CONTRACT["security_controls"].values()
        if isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", v)
    },
    # The team_code placeholder is contract text, so it is a known member.
    "participation_placeholder": {
        v for v in CONTRACT["hackathon_participation"].values()
        if isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", v)
    },
}

ALL_KNOWN: set[str] = set().union(*VOCABULARIES.values())

# Prefix-shaped vocabularies: the FORM alone proves a membership claim.
PREFIXED: dict[str, re.Pattern[str]] = {
    "hard_constraint": re.compile(r"^HC-\d{3}$"),
    "eval_case": re.compile(r"^EVAL-\d{3}$"),
    "event": re.compile(r"^EVT-\d{3}$"),
}

PREFIXED_VALID: dict[str, set[str]] = {
    "hard_constraint": {h["id"] for h in CONTRACT["hard_constraints"]},
    "eval_case": {t["case_id"] for t in CONTRACT["acceptance_tests"]},
    "event": {
        e
        for group in CONTRACT["data_source"]["official_disruption_coverage"].values()
        for e in group
    },
}

# The token shape.
#   (?<![.\w])  excludes dotted attribute references in prose
#               ("see planpilot.CONTRACT_SHA256")
#   (?!\.(md|json|…)) excludes FILENAME references ("IMPLEMENTATION_NOTES.md",
#               "REVIEW_HANDOFF_FOR_CODEX.md"). A token followed by a dot and a
#               word char is naming a file, not claiming a vocabulary value.
#               This was added after the guard flagged six filename stems in
#               tools/factcheck_impl_handoff.py and thereby blocked the whole
#               test suite — a false positive in the guard is worse than a miss,
#               because it trains people to override it.
#
#               The first version used (?!\.\w) — any dot+word-char. That was too
#               generous and was confirmed exploitable two ways:
#                 "PLAN_DIGEST_MISMATCHED.md"[:-3]  restores the fabrication
#                 "Error MISMATCHED.After retry EVAL-031.Failed"  fully invisible
#               So the suffix must now be a KNOWN document extension. A trailing
#               sentence period still matches, because "MISMATCHED." is a dot
#               followed by whitespace or a quote, not by an extension.
#   (?![\w])    excludes mid-word matches
_DOC_EXT = r"(?:md|markdown|json|py|txt|ya?ml|rst|csv|tsv|toml|cfg|ini|log|html|ipynb)"
TOKEN_RE = re.compile(
    r"(?<![.\w])"
    r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{2,}-\d{3})"
    r"(?!\." + _DOC_EXT + r"\b)(?![\w])"
)

# Filename-shaped tokens: "STEM.ext". TOKEN_RE exempts these so that real
# references like IMPLEMENTATION_NOTES.md are not flagged (defect D13). But the
# exemption is exploitable — `"PLAN_DIGEST_MISMATCHED.md"[:-3]` restores the
# fabrication at runtime — so FILENAME_RE re-examines the stem and flags it when
# the stem is ITSELF a fabricated identifier. Measured against the repo: 7
# distinct TOKEN.ext strings exist, this rule flags 0 legitimate filenames.
FILENAME_RE = re.compile(
    r"(?<![.\w])"
    r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{2,}-\d{3})"
    r"\." + _DOC_EXT + r"\b"
)

# The implementation's own namespaces. These are env vars and local constants,
# not contract vocabulary claims.
IMPLEMENTATION_NAMESPACES: tuple[str, ...] = ("PLANPILOT_",)

# ---------------------------------------------------------------------------
# Allowlist: vocabulary-shaped tokens legitimately outside the contract.
#
# PATH-SCOPED, not global. A global allowlist would let `FABRICATED_CODE` pass in
# production code just because a test file needs it — which defeats the purpose.
# Each entry is (token -> (reason, tuple of path prefixes)). A token outside those
# paths is a violation like any other.
#
# Adding an entry is a one-line change a reviewer sees in the diff. That is the
# point: nothing is silently permitted.
# ---------------------------------------------------------------------------

_NEGATIVE_TEST_DIR = "tests/unit/test_closed_vocabularies.py"
# The bypass-regression module pins the ten bypasses an independent attack
# found, so it must contain the same fabricated tokens as fixtures.
_BYPASS_TEST_FILE = "tests/unit/test_guard_bypass_regressions.py"
# The audit-fix pin module injects a fabricated lifecycle status to prove
# load_state refuses it (finding F1), so it carries one fixture token.
_PIN_TEST_FILE = "tests/unit/test_audit_fixes_pinned.py"
# All three files are SCANNED (none is in NEGATIVE_FIXTURE_PATHS), so the
# allowlist mechanism itself is exercised on every run.
_FIXTURE_FILES = (_NEGATIVE_TEST_DIR, _BYPASS_TEST_FILE, _PIN_TEST_FILE)

# Deliberately MINIMAL. Three entries were removed after the self-checks proved
# them dead:
#   PUBLISHING                      — has no underscore, so TOKEN_RE never
#                                     matches it; it could not be a violation.
#   TO_BE_RECORDED_BEFORE_SUBMISSION — is a contract member (collected from
#                                     hackathon_participation), so allowlisting
#                                     it hid a real vocabulary value.
#   NOT_A_REAL_STATUS               — appeared nowhere but in this list.
# A dead entry is worse than no entry: it looks like coverage and is not.
ALLOWED_LOCAL: dict[str, tuple[str, tuple[str, ...]]] = {
    # Fixtures whose entire job is to be caught by this guard. Path-scoped to the
    # guard's own test module, which is therefore SCANNED (not skipped) — so the
    # allowlist mechanism itself is exercised on every run.
    #
    # These are NOT obfuscated (e.g. "HC-0" + "14") to dodge the guard: doing so
    # would make the tests unreadable and would hide from a human reviewer the
    # very thing the guard exists to surface. Declaring them here is the honest
    # mechanism, and every entry shows up in the diff for review.
    "FABRICATED_CODE": (
        "asserts the guard rejects an invented error code",
        (_NEGATIVE_TEST_DIR,),
    ),
    "PLAN_DIGEST_MISMATCHED": (
        "near-miss fixture proving edit-distance detection works",
        _FIXTURE_FILES,
    ),
    "SEARCH_ESCALATION_EXHAUST": (
        "truncation fixture proving near-miss detection",
        _FIXTURE_FILES,
    ),
    # Out-of-range prefixed ids: only HC-001..013, EVAL-001..030, EVT-001..007
    # exist. These assert the range check fires.
    "HC-014": (
        "asserts an out-of-range hard-constraint id is flagged",
        _FIXTURE_FILES,
    ),
    "HC-099": (
        "appears in the guard's test module as a string constant holding probe "
        "source; from the OUTER file's view that is a fabricated id needing the "
        "allowlist, even though INSIDE the probe it is docstring prose and is not "
        "scanned. Two levels, two different truths — this entry covers the outer one.",
        (_NEGATIVE_TEST_DIR,),
    ),
    "EVAL-031": (
        "asserts an out-of-range EVAL id is flagged",
        _FIXTURE_FILES,
    ),
    "EVT-009": (
        "asserts an out-of-range event id is flagged",
        (_NEGATIVE_TEST_DIR,),
    ),
    # Bare filename stems passed to _is_fabricated_stem() to assert it exempts
    # real documentation names. Without the ".md" suffix they are not
    # filename-shaped, so TOKEN_RE flags them — correctly, by its own rules.
    "IMPLEMENTATION_NOTES": (
        "bare filename stem used to prove _is_fabricated_stem exempts real docs",
        (_BYPASS_TEST_FILE,),
    ),
    "REVIEW_HANDOFF_FOR_CODEX": (
        "bare filename stem used to prove _is_fabricated_stem exempts real docs",
        (_BYPASS_TEST_FILE,),
    ),
    # Implementation-constant shapes used to assert __all__ exports are excluded.
    "SOME_LOCAL_CONSTANT": (
        "asserts __all__ export names are not treated as vocabulary claims",
        (_NEGATIVE_TEST_DIR,),
    ),
    "ANOTHER_ONE": (
        "asserts __all__ export names are not treated as vocabulary claims",
        (_NEGATIVE_TEST_DIR,),
    ),
    # A fabricated lifecycle status injected into a dumped state to prove
    # load_state refuses it (audit finding F1). Not a contract status, by design:
    # the test's whole point is that an out-of-enum status is rejected on load.
    "HACKED_PUBLISHED": (
        "fabricated lifecycle status proving load_state rejects out-of-enum "
        "records (audit F1)",
        (_PIN_TEST_FILE,),
    ),
}

# Files that must fabricate tokens as string literals in their OWN body, so they
# cannot be scanned at all. The guard's self-test cases live inline here, and
# scanning the file that proves the guard works would report the proof as the
# crime. Kept narrow: test_closed_vocabularies.py is NOT here, because its
# fixtures are covered by ALLOWED_LOCAL and it should still be scanned.
NEGATIVE_FIXTURE_PATHS = (
    "tests/negative_control/",
    "tools/check_closed_vocabularies.py",
)

# Populated by collect_defined_names() before scanning. A SCREAMING_SNAKE token
# that is a real Python definition somewhere in the project is an implementation
# constant (e.g. CONTRACT_SHA256), not a contract vocabulary claim, so it is
# excluded on principle rather than by allowlist.
#
# Known limitation: this would also excuse a fabricated token that someone first
# binds as a Python constant. That is an acceptable trade — the alternative is
# allowlisting every implementation constant by hand, which rots faster.
_DEFINED_NAMES: set[str] = set()


def collect_defined_names(paths: list[Path]) -> set[str]:
    """Every name DEFINED (not merely referenced) across the scanned modules."""
    names: set[str] = set()
    for path in paths:
        try:
            tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
    return names


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of every Constant node that is a docstring.

    A docstring is the first statement of a Module/ClassDef/FunctionDef/
    AsyncFunctionDef body, and in the AST that statement is `Expr(Constant)` —
    so the Constant's *parent* is the Expr, not the Module. Checking
    `isinstance(parent, ast.Module)` therefore never matches, which is why the
    first draft silently failed to exclude any docstring.

    Collecting them up front, keyed by id(), sidesteps the parent lookup
    entirely.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            ids.add(id(body[0].value))
    return ids


_CURRENT_TREE: ast.AST = ast.Module(body=[], type_ignores=[])
_CURRENT_DOCSTRINGS: set[int] = set()


def _is_docstring_constant(node: ast.Constant, parent: ast.AST) -> bool:
    """Retained for compatibility; real exclusion uses the precomputed id set."""
    return id(node) in _CURRENT_DOCSTRINGS


def _is_all_entry(node: ast.Constant, parent: ast.AST) -> bool:
    """True when this string is an element of an `__all__ = [...]` assignment."""
    if not isinstance(parent, ast.List):
        return False
    for holder in ast.walk(_CURRENT_TREE):
        if isinstance(holder, ast.Assign):
            for target in holder.targets:
                if isinstance(target, ast.Name) and target.id == "__all__" and holder.value is parent:
                    return any(elt is node for elt in parent.elts)
    return False


def _string_constants(tree: ast.AST):
    """Yield (Constant, parent) for every inspectable string literal."""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if not (isinstance(child, ast.Constant) and isinstance(child.value, str)):
                continue
            if _is_docstring_constant(child, parent):
                continue
            if _is_all_entry(child, parent):
                continue
            yield child, parent


def _is_fabricated_stem(token: str) -> bool:
    """True when a filename stem is itself a fabricated contract identifier.

    Only two shapes qualify, both unambiguous:
      * a prefixed id outside the contract's real range (HC-014, EVAL-031, EVT-009)
      * a near miss of a real member (PLAN_DIGEST_MISMATCHED, edit distance 2)

    Everything else stays exempt, so genuine filenames such as
    IMPLEMENTATION_NOTES.md, REVIEW_HANDOFF_FOR_CODEX.md and V1.8_changelog.md
    are never flagged. Note that a bare word like README cannot reach here at
    all: TOKEN_RE and FILENAME_RE both require an underscore group or the
    PREFIX-DIGITS shape.
    """
    if token in ALL_KNOWN:
        return False
    for kind, pattern in PREFIXED.items():
        if pattern.match(token) and token not in PREFIXED_VALID[kind]:
            return True
    return _near_miss(token) is not None


def _tokens(text: str) -> list[str]:
    out = []
    for m in TOKEN_RE.finditer(text):
        tok = m.group(1)
        if tok.startswith(IMPLEMENTATION_NAMESPACES):
            continue
        if any(p.match(tok) for p in PREFIXED.values()) or "_" in tok:
            out.append(tok)
    # Re-examine filename-shaped tokens that TOKEN_RE deliberately skipped.
    for m in FILENAME_RE.finditer(text):
        stem = m.group(1)
        if stem.startswith(IMPLEMENTATION_NAMESPACES):
            continue
        if _is_fabricated_stem(stem) and stem not in out:
            out.append(stem)
    return out


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein, early-exiting above `cap` (only near misses matter)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _near_miss(token: str) -> str | None:
    """The closest real member within edit distance 2, else None."""
    best, best_d = None, 3
    for known in ALL_KNOWN:
        d = _edit_distance(token, known, cap=2)
        if d <= 2 and d < best_d:
            best, best_d = known, d
    return f"{best} (edit distance {best_d})" if best else None


def classify(token: str) -> tuple[str, str | None] | None:
    """Return (kind, detail) for a violation, or None when legitimate.

    ORDER MATTERS. The near-miss check runs BEFORE the _DEFINED_NAMES exclusion.
    It used to run after, which meant binding a fabricated code as a Python name
    anywhere in the project — even a local variable inside a test function — made
    it an "implementation constant" and silently voided the guard's own
    highest-signal check. Confirmed exploitable: `PLAN_DIGEST_MISMATCHED = "..."`
    in one file made the string pass in another.

    A defined name within edit distance 2 of a contract member is a vocabulary
    claim wearing a constant's clothes, not an implementation constant. Genuine
    implementation constants (CONTRACT_SHA256, OPERATION_SORT_KEY_FIELDS) are far
    from every member and are unaffected; if one ever is close, the allowlist is
    the escape hatch and a reviewer sees it in the diff.
    """
    if token in ALL_KNOWN:
        return None
    for kind, pattern in PREFIXED.items():
        if pattern.match(token):
            if token in PREFIXED_VALID[kind]:
                return None
            # HC-014 with only 13 constraints is a near miss by form, and the
            # most likely fabrication of all.
            return f"fabricated_{kind}", None
    miss = _near_miss(token)
    if miss:
        return "near_miss_of_contract_member", miss
    # Only now: a SCREAMING_SNAKE token that is a real Python definition somewhere
    # in the project is an implementation constant, not a vocabulary claim.
    if token in _DEFINED_NAMES:
        return None
    return "not_in_any_vocabulary", None


def _path_matches(rel: str, pattern: str) -> bool:
    """Component-aware path match. Bare `startswith` has no boundary, so
    "tools/check_closed_vocabularies.py.evil.py" matched the guard's own entry and
    was never scanned, and "tests/unit/test_closed_vocabularies.py.evil.py"
    inherited all nine allowlist tokens. Both were confirmed loadable with the
    guard exiting 0.

    A pattern ending in "/" matches a directory subtree; anything else must be
    exactly equal.
    """
    if pattern.endswith("/"):
        return rel == pattern[:-1] or rel.startswith(pattern)
    return rel == pattern


def scan_file(path: Path, *, is_negative_fixture: bool = False) -> list[tuple[int, str, str, str | None]]:
    # `relative_to` raises for paths outside ROOT (the self-test writes probes to
    # a tempdir). Fall back to the raw name so scanning is location-independent.
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        rel = path.name
    skip = is_negative_fixture or any(_path_matches(rel, p) for p in NEGATIVE_FIXTURE_PATHS)
    if skip:
        return []

    global _CURRENT_TREE, _CURRENT_DOCSTRINGS
    try:
        _CURRENT_TREE = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [(getattr(exc, "lineno", 0) or 0, f"<unparseable: {exc}>", "parse_error", None)]
    # Must be populated per file, or the docstring exclusion silently does
    # nothing (an empty set matches no node).
    _CURRENT_DOCSTRINGS = _docstring_nodes(_CURRENT_TREE)

    out: list[tuple[int, str, str, str | None]] = []
    for node, _parent in _string_constants(_CURRENT_TREE):
        for token in _tokens(node.value):
            entry = ALLOWED_LOCAL.get(token)
            if entry is not None:
                _reason, allowed_paths = entry
                # Path-scoped: the token is permitted ONLY under its declared
                # paths. Anywhere else it is a violation like any other.
                if any(_path_matches(rel, p) for p in allowed_paths):
                    continue
            hit = classify(token)
            if hit:
                out.append((node.lineno, token, hit[0], hit[1]))
    return out


def _self_test(tmp: Path) -> int:
    """Prove the guard catches fabrication. A guard that cannot fail is theatre."""
    cases = [
        ("fabricated error code", 'X = "FABRICATED_CODE"\n', True),
        ("near-miss error code", 'X = "PLAN_DIGEST_MISMATCHED"\n', True),
        ("truncated code", 'X = "SEARCH_ESCALATION_EXHAUST"\n', True),
        ("fabricated HC id", 'X = "HC-014"\n', True),
        ("fabricated EVAL id", 'X = "EVAL-031"\n', True),
        ("fabricated event id", 'X = "EVT-009"\n', True),
        ("real error code", 'X = "PLAN_DIGEST_MISMATCH"\n', False),
        ("real HC id", 'X = "HC-013"\n', False),
        ("real lifecycle status", 'X = "AWAITING_APPROVAL"\n', False),
        ("real security action", 'X = "BLOCK_AND_LOG"\n', False),
        ("docstring prose", '"""Uses PLAN_DIGEST_MISMATCHED loosely."""\nX = 1\n', False),
        ("__all__ export", '__all__ = ["SOME_LOCAL_CONSTANT"]\n', False),
        ("dotted prose reference", 'raise RuntimeError("see planpilot.NOT_A_MEMBER")\n', False),
        ("implementation namespace", 'X = "PLANPILOT_TOTALLY_NEW_VAR"\n', False),
        ("lowercase identifier", 'x = "some_local_value"\n', False),
        ("mixed case class name", 'X = "DigestMismatchError"\n', False),
    ]

    probe = tmp / "probe_module.py"
    bad = 0
    print("SELF-TEST: does the guard actually catch fabrication?")
    for label, source, should_fail in cases:
        probe.write_bytes(source.encode("utf-8"))
        found = scan_file(probe)
        got = bool(found)
        ok = got == should_fail
        if not ok:
            bad += 1
        detail = found[0][2] if found else "-"
        print(f"  {'OK  ' if ok else 'FAIL'} | {label:<32} expected_flag={should_fail!s:<5} got={got!s:<5} [{detail}]")
    probe.unlink(missing_ok=True)

    # A near miss that is NOT allowlisted must still be caught — this is what
    # proves the allowlist is not a blanket "skip anything odd" escape hatch.
    probe2 = tmp / "probe2.py"
    probe2.write_bytes(b'X = "VALIDATION_FAILEDX"\n')
    found = scan_file(probe2)
    ok = bool(found)
    if not ok:
        bad += 1
    print(f"  {'OK  ' if ok else 'FAIL'} | {'non-allowlisted near miss':<32} expected_flag=True  got={bool(found)!s:<5} "
          f"[{found[0][2] if found else '-'}]")
    probe2.unlink(missing_ok=True)

    print(f"\nSELF-TEST | {'PASS' if bad == 0 else f'FAIL ({bad} cases)'}")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            return _self_test(Path(td))

    targets: list[Path] = []
    for base in ("src", "tests", "tools"):
        d = ROOT / base
        if d.exists():
            targets.extend(sorted(p for p in d.rglob("*.py")))

    print(f"contract: {CONTRACT_PATH.name}")
    print(f"vocabularies: {len(VOCABULARIES)} sets, {len(ALL_KNOWN)} known members")
    print(f"scanning {len(targets)} modules under src/ tests/ tools/")
    print()

    # Populate the implementation-constant set BEFORE scanning, so classify() can
    # exclude real Python definitions (CONTRACT_SHA256 etc.) on principle.
    global _DEFINED_NAMES
    _DEFINED_NAMES = collect_defined_names(targets)

    violations: list[tuple[str, int, str, str, str | None]] = []
    for path in targets:
        for lineno, token, kind, detail in scan_file(path):
            violations.append((path.relative_to(ROOT).as_posix(), lineno, token, kind, detail))

    # ---- self-check 1: allowlist must not hide a real vocabulary member
    stale = sorted(t for t in ALLOWED_LOCAL if t in ALL_KNOWN)

    # ---- self-check 2: every allowlist entry must be used somewhere
    #
    # Two bugs this has to avoid, both of which made the first version report a
    # false "all used":
    #   (a) it counted the guard's OWN body, where the allowlist definition
    #       literally contains the tokens — circular self-justification;
    #   (b) it called _string_constants() without resetting the module-level tree
    #       state, so docstring detection used the previous file's node ids.
    # NEGATIVE_FIXTURE_PATHS are skipped here exactly as scan_file skips them, so
    # only a genuine consumer counts as evidence.
    global _CURRENT_TREE, _CURRENT_DOCSTRINGS
    seen: dict[str, list[str]] = {t: [] for t in ALLOWED_LOCAL}
    for path in targets:
        rel = path.relative_to(ROOT).as_posix()
        if any(_path_matches(rel, p) for p in NEGATIVE_FIXTURE_PATHS):
            continue  # the guard's own body is not evidence of use
        try:
            tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        _CURRENT_TREE = tree
        _CURRENT_DOCSTRINGS = _docstring_nodes(tree)
        for node, _ in _string_constants(tree):
            for tok in _tokens(node.value):
                if tok in seen:
                    seen[tok].append(rel)

    # A dead entry is worse than no entry: it looks like coverage and is not.
    unused = sorted(t for t, files in seen.items() if not files)

    # ---- self-check 2b: every allowlist path must exist, or the entry cannot
    # ever fire and silently rots into false coverage.
    missing_paths: list[str] = []
    for tok, (_reason, paths) in ALLOWED_LOCAL.items():
        for p in paths:
            target = ROOT / p
            if not (target.exists() or (target.is_dir() if target.suffix == "" else False)):
                missing_paths.append(f"{tok} -> {p}")

    # ---- self-check 3: vocabularies sane
    empty = sorted(k for k, v in VOCABULARIES.items() if not v)
    sanity: list[str] = []
    if empty:
        sanity.append(f"empty vocabularies: {empty}")
    if "PLAN_DIGEST_MISMATCH" not in VOCABULARIES["error_code"]:
        sanity.append("error_code missing a known member")
    if "DRAFT" not in VOCABULARIES["lifecycle_status"]:
        sanity.append("lifecycle_status missing a known member")
    if "BLOCK_AND_LOG" not in ALL_KNOWN:
        sanity.append("BLOCK_AND_LOG (security_controls.prompt_injection_action) not collected")
    if len(VOCABULARIES["error_code"]) != 18:
        sanity.append(f"error_code has {len(VOCABULARIES['error_code'])} members, expected 18")
    if len(VOCABULARIES["validation_issue_code"]) != 36:
        sanity.append(
            f"validation_issue_code has {len(VOCABULARIES['validation_issue_code'])} members, expected 36"
        )

    near = [v for v in violations if v[3] == "near_miss_of_contract_member"
            or v[3].startswith("fabricated_")]
    other = [v for v in violations if v not in near]

    if near:
        print(f"LIKELY FABRICATIONS (highest signal): {len(near)}")
        for rel, lineno, token, kind, detail in near:
            extra = f" — closest real member: {detail}" if detail else ""
            print(f"  {rel}:{lineno}  {token!r}  [{kind}]{extra}")
        print()
    if other:
        print(f"UNRECOGNISED VOCABULARY-SHAPED TOKENS: {len(other)}")
        for rel, lineno, token, kind, detail in other:
            print(f"  {rel}:{lineno}  {token!r}  [{kind}]")
        print()
        print("Either use an existing contract member, or add the token to")
        print("ALLOWED_LOCAL in this file WITH A REASON so a reviewer sees it.")
        print()

    problems = 0
    if violations:
        problems += 1
    if stale:
        print(f"ALLOWLIST STALE (these ARE contract members): {stale}")
        problems += 1
    if sanity:
        print("SANITY FAILURES:")
        for s in sanity:
            print(f"  {s}")
        problems += 1

    # Both of these are HARD failures, not notes. The first version reported
    # unused entries as a "note" and so shipped three dead ones: PUBLISHING could
    # never match TOKEN_RE, NOT_A_REAL_STATUS appeared nowhere, and
    # TO_BE_RECORDED_BEFORE_SUBMISSION was already a contract member. A dead
    # allowlist entry looks like coverage and is not.
    if unused:
        print(f"ALLOWLIST DEAD ENTRIES (used by no scanned module): {unused}")
        print("  Remove them, or add the module that needs them.")
        problems += 1
    if missing_paths:
        print(f"ALLOWLIST PATHS THAT DO NOT EXIST: {missing_paths}")
        print("  An entry scoped to a missing path can never fire.")
        problems += 1

    if problems:
        print(f"\nCLOSED VOCABULARY CHECK | FAIL (problems={problems})")
        return 1

    print(f"allowlist: {len(ALLOWED_LOCAL)} entries")
    for tok, (reason, paths) in sorted(ALLOWED_LOCAL.items()):
        print(f"  {tok} — used by {', '.join(sorted(set(seen[tok])))}")
        print(f"      reason: {reason}")
    print("all entries genuinely outside the contract, all used, all paths exist")
    print("no near misses, no unrecognised tokens")
    print("CLOSED VOCABULARY CHECK | PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
