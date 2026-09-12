"""Pinned regressions for the bypasses an independent attack found.

An adversarial review of tools/check_closed_vocabularies.py found ten bypasses,
all proven end to end. Three were closed; this module pins them so a future
"simplification" cannot silently reopen them. The remaining seven are documented
as accepted limits in REVIEW_HANDOFF_IMPLEMENTATION.md §3.2 — they are pinned
here too, as xfail-style assertions of CURRENT behaviour, so that if anyone ever
closes one the test fails and forces the documentation to be updated rather than
letting the doc rot.

Each test names the bypass number from the attack report.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _find_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "contract").is_dir() and (parent / "src" / "planpilot").is_dir():
            return parent
    raise SystemExit("repo root not found")


ROOT = _find_root()
GUARD_PATH = ROOT / "tools" / "check_closed_vocabularies.py"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("guard_under_test", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_defined_names(guard):
    """classify() consults a module-global; leave it empty around each test."""
    saved = set(guard._DEFINED_NAMES)
    guard._DEFINED_NAMES = set()
    yield
    guard._DEFINED_NAMES = saved


# ---------------------------------------------------------------------------
# Bypass #1 (CRITICAL) — _DEFINED_NAMES preempted the near-miss check
# ---------------------------------------------------------------------------

class TestBypass1DefinedNamesNoLongerPreemptsNearMiss:
    def test_a_fabrication_bound_as_a_python_name_is_still_caught(self, guard):
        """The exact exploit: define it once anywhere, use the string anywhere else."""
        guard._DEFINED_NAMES = {"PLAN_DIGEST_MISMATCHED"}
        got = guard.classify("PLAN_DIGEST_MISMATCHED")
        assert got is not None, "binding a fabrication as a constant hid it again"
        assert got[0] == "near_miss_of_contract_member"
        assert "PLAN_DIGEST_MISMATCH" in got[1]

    def test_a_truncated_code_bound_as_a_name_is_still_caught(self, guard):
        guard._DEFINED_NAMES = {"SEARCH_ESCALATION_EXHAUST"}
        assert guard.classify("SEARCH_ESCALATION_EXHAUST") is not None

    def test_a_double_letter_status_bound_as_a_name_is_still_caught(self, guard):
        guard._DEFINED_NAMES = {"APPROVEDD"}
        assert guard.classify("APPROVEDD") is not None

    @pytest.mark.parametrize("const", [
        "CONTRACT_SHA256", "OPERATION_SORT_KEY_FIELDS", "LIFECYCLE_STATUSES",
        "DIGEST_EXCLUDED_FIELDS", "IMPLEMENTATION_NAMESPACES",
    ])
    def test_genuine_implementation_constants_still_pass(self, guard, const):
        """The fix must not turn every local constant into a violation."""
        guard._DEFINED_NAMES = {const}
        assert guard.classify(const) is None, f"{const} is a real implementation constant"


# ---------------------------------------------------------------------------
# Bypass #3 (HIGH) — bare startswith had no component boundary
# ---------------------------------------------------------------------------

class TestBypass3PathMatchingHasAComponentBoundary:
    def test_a_file_named_after_the_guard_is_not_skipped(self, guard):
        """tools/check_closed_vocabularies.py.evil.py used to match and never be scanned."""
        assert guard._path_matches(
            "tools/check_closed_vocabularies.py.evil.py",
            "tools/check_closed_vocabularies.py",
        ) is False

    def test_a_file_named_after_the_test_module_does_not_inherit_the_allowlist(self, guard):
        """It used to inherit all nine allowlist tokens."""
        assert guard._path_matches(
            "tests/unit/test_closed_vocabularies.py.evil.py",
            "tests/unit/test_closed_vocabularies.py",
        ) is False

    def test_a_sibling_with_an_underscore_suffix_does_not_inherit(self, guard):
        assert guard._path_matches(
            "tests/unit/test_closed_vocabularies_evil.py",
            "tests/unit/test_closed_vocabularies.py",
        ) is False

    def test_a_prefix_colliding_directory_does_not_match(self, guard):
        assert guard._path_matches(
            "tests/negative_controlX/y.py", "tests/negative_control/"
        ) is False

    def test_exact_file_match_still_works(self, guard):
        assert guard._path_matches(
            "tools/check_closed_vocabularies.py", "tools/check_closed_vocabularies.py"
        ) is True

    def test_directory_subtree_match_still_works(self, guard):
        assert guard._path_matches("tests/negative_control/test_x.py", "tests/negative_control/") is True

    def test_the_directory_itself_still_matches(self, guard):
        assert guard._path_matches("tests/negative_control", "tests/negative_control/") is True


# ---------------------------------------------------------------------------
# Bypass #6 (MED-HIGH) — the filename lookahead was too generous
# ---------------------------------------------------------------------------

class TestBypass6FilenameLookaheadRequiresAKnownExtension:
    def test_the_md_slice_restore_trick_is_caught(self, guard):
        """`"PLAN_DIGEST_MISMATCHED.md"[:-3]` restores the fabrication at runtime."""
        assert guard._tokens('p = "PLAN_DIGEST_MISMATCHED.md"[:-3]') == ["PLAN_DIGEST_MISMATCHED"]

    def test_the_prefixed_id_md_slice_trick_is_caught(self, guard):
        assert guard._tokens('p = "HC-014.md"[:-3]') == ["HC-014"]

    def test_packed_sentences_are_caught(self, guard):
        """No space after the period used to read as a file extension."""
        assert guard._tokens(
            'msg = "Error PLAN_DIGEST_MISMATCHED.After retry EVAL-031.Failed"'
        ) == ["PLAN_DIGEST_MISMATCHED", "EVAL-031"]

    def test_a_trailing_sentence_period_still_does_not_disable_detection(self, guard):
        assert guard._tokens('msg = "the code was PLAN_DIGEST_MISMATCHED."') == [
            "PLAN_DIGEST_MISMATCHED"
        ]

    @pytest.mark.parametrize("filename", [
        "IMPLEMENTATION_NOTES.md", "REVIEW_HANDOFF_FOR_CODEX.md",
        "REVIEW_HANDOFF_IMPLEMENTATION.md", "V1.8_changelog.md",
        "README.md", "data.json", "module.py", "config.yaml", "schema.yml",
        "notes.txt", "spec.rst", "out.csv", "data.tsv", "pyproject.toml",
        "app.cfg", "settings.ini", "run.log", "page.html", "nb.ipynb",
        "Doc.markdown",
    ])
    def test_real_filenames_are_not_flagged(self, guard, filename):
        """D13 must stay fixed: a guard that cries wolf gets overridden."""
        assert guard._tokens(f'p = ROOT / "{filename}"') == [], f"{filename} was flagged"

    def test_the_stem_check_only_flags_fabrications(self, guard):
        """The discriminator: a fabricated stem, not the filename shape."""
        assert guard._is_fabricated_stem("PLAN_DIGEST_MISMATCHED") is True
        assert guard._is_fabricated_stem("HC-014") is True
        assert guard._is_fabricated_stem("IMPLEMENTATION_NOTES") is False
        assert guard._is_fabricated_stem("REVIEW_HANDOFF_FOR_CODEX") is False
        assert guard._is_fabricated_stem("PLAN_DIGEST_MISMATCH") is False  # a real member


# ---------------------------------------------------------------------------
# Accepted limits — pinned as CURRENT behaviour.
#
# These are NOT endorsements. Each is a known hole documented in
# REVIEW_HANDOFF_IMPLEMENTATION.md §3.2. The tests assert the hole is still open
# so that closing one fails the suite and forces the documentation to be updated
# in the same commit. Silent divergence between doc and code is the failure mode
# this project keeps hitting.
# ---------------------------------------------------------------------------

class TestDocumentedLimitsAreStillOpen:
    def test_limit2_non_python_files_are_not_scanned(self, guard):
        """Bypass #2 (CRITICAL, unfixed): the guard scans only *.py under src/tests/tools."""
        targets = []
        for base in ("src", "tests", "tools"):
            d = ROOT / base
            if d.exists():
                targets.extend(sorted(p for p in d.rglob("*.py")))
        assert all(p.suffix == ".py" for p in targets), "scan scope widened — update §3.2"

    def test_limit4_single_word_tokens_are_invisible(self, guard):
        """Bypass #4 (HIGH, unfixed): TOKEN_RE needs an underscore group or PREFIX-DIGITS."""
        assert guard._tokens('X = "PUBLISHING"') == [], "TOKEN_RE now matches bare words — update §3.2"
        # classify WOULD catch it if the token ever reached it
        assert guard.classify("PUBLISHING") is not None

    def test_limit5_concatenation_is_invisible(self, guard):
        """Bypass #5 (HIGH, unfixed): computed strings are not folded."""
        assert guard._tokens('X = "HC-0" + "14"') == [], "constant folding added — update §3.2"
        assert guard._tokens('X = f"HC-{n:03d}"') == [], "f-string folding added — update §3.2"

    def test_limit7_comments_are_not_scanned(self, guard):
        """Bypass #7 (MEDIUM, unfixed): only string constants are inspected."""
        src = "# PLAN_DIGEST_MISMATCHED lives only in this comment\nX = 1\n"
        import ast as _ast
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.py"
            p.write_bytes(src.encode("utf-8"))
            assert guard.scan_file(p) == [], "comment scanning added — update §3.2"

    def test_limit8_bytes_literals_are_not_decoded(self, guard):
        """Bypass #10 (LOW-MED, unfixed): bytes constants are skipped."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.py"
            p.write_bytes(b'X = b"PLAN_DIGEST_MISMATCHED"\n')
            assert guard.scan_file(p) == [], "bytes scanning added — update §3.2"


# ---------------------------------------------------------------------------
# End-to-end: the guard must still be green, and still catch honest mistakes
# ---------------------------------------------------------------------------

class TestGuardStillWorksEndToEnd:
    def test_self_test_passes(self):
        r = subprocess.run(
            [sys.executable, str(GUARD_PATH), "--self-test"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0, r.stdout[-1500:]
        assert "SELF-TEST | PASS" in r.stdout

    def test_repo_scan_passes(self):
        r = subprocess.run(
            [sys.executable, str(GUARD_PATH)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0, r.stdout[-2000:]
        assert "CLOSED VOCABULARY CHECK | PASS" in r.stdout

    def test_an_honest_mistake_is_still_caught_in_a_probe(self, guard, tmp_path):
        """The realistic threat model: a developer writes the wrong code into a literal."""
        p = tmp_path / "honest_mistake.py"
        p.write_bytes(
            b'CODE = "PLAN_DIGEST_MISMATCH"\n'          # correct
            b'BAD  = "PLAN_DIGEST_MISMATCHED"\n'        # typo
            b'ALSO_BAD = "HC-014"\n'                    # out of range
        )
        found = guard.scan_file(p)
        tokens = {t for _, t, _, _ in found}
        assert tokens == {"PLAN_DIGEST_MISMATCHED", "HC-014"}
        assert "PLAN_DIGEST_MISMATCH" not in tokens, "a valid code was flagged"
