"""Task 4.2 — the closed-vocabulary guard must actually catch fabrication.

This module is SCANNED by tools/check_closed_vocabularies.py (it is deliberately
not in NEGATIVE_FIXTURE_PATHS), which means two things happen on every run:

  1. The three fixture tokens below exercise the path-scoped allowlist. If the
     allowlist mechanism broke, this file would fail the guard.
  2. The guard's own self-checks (dead entries, missing paths) are satisfied by
     this file existing. Delete it and the guard fails.

The fixtures are fabricated on purpose. Do not "fix" them.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "tools" / "check_closed_vocabularies.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("closed_vocab_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load_guard()


@pytest.fixture
def probe(tmp_path, guard):
    """Write a throwaway module and scan it. Returns a callable."""

    def _run(source: str):
        path = tmp_path / "probe_module.py"
        path.write_bytes(textwrap.dedent(source).encode("utf-8"))
        # The guard keeps module-level tree state; scan_file resets it per file.
        return guard.scan_file(path)

    return _run


# ---------------------------------------------------------------------------
# The three allowlisted fixtures. Their presence here is what makes the
# allowlist entries live rather than dead.
# ---------------------------------------------------------------------------

FABRICATED_TOKENS = (
    "FABRICATED_CODE",            # an invented error code
    "PLAN_DIGEST_MISMATCHED",     # near miss of PLAN_DIGEST_MISMATCH
    "SEARCH_ESCALATION_EXHAUST",  # truncation of SEARCH_ESCALATION_EXHAUSTED
)


class TestGuardCatchesFabrication:
    @pytest.mark.parametrize("token", FABRICATED_TOKENS)
    def test_a_fabricated_token_outside_the_allowlist_path_is_flagged(self, probe, token):
        """Written into a temp module (not the allowlisted path) it MUST be caught.

        This is the check that proves the allowlist is path-scoped rather than a
        global "ignore this word" escape hatch.
        """
        found = probe(f'X = "{token}"\n')
        assert found, f"{token} was not flagged outside its allowlisted path"
        assert found[0][1] == token

    def test_fabricated_code_is_unrecognised(self, probe):
        found = probe('X = "FABRICATED_CODE"\n')
        assert found[0][2] == "not_in_any_vocabulary"

    @pytest.mark.parametrize("token,closest", [
        ("PLAN_DIGEST_MISMATCHED", "PLAN_DIGEST_MISMATCH"),
        ("SEARCH_ESCALATION_EXHAUST", "SEARCH_ESCALATION_EXHAUSTED"),
    ])
    def test_near_misses_report_the_closest_real_member(self, probe, token, closest):
        """The dangerous fabrication reads as legitimate; it must name the real one."""
        found = probe(f'X = "{token}"\n')
        assert found, f"{token} was not flagged"
        kind, detail = found[0][2], found[0][3]
        assert kind == "near_miss_of_contract_member"
        assert closest in detail, f"{token} should point at {closest}, got {detail!r}"

    @pytest.mark.parametrize("token,kind", [
        ("HC-014", "fabricated_hard_constraint"),   # only HC-001..HC-013 exist
        ("EVAL-031", "fabricated_eval_case"),       # only EVAL-001..EVAL-030 exist
        ("EVT-009", "fabricated_event"),            # only EVT-001..EVT-007 exist
    ])
    def test_out_of_range_prefixed_ids_are_flagged(self, probe, token, kind):
        found = probe(f'X = "{token}"\n')
        assert found, f"{token} was not flagged"
        assert found[0][2] == kind

    def test_unparseable_module_is_reported_not_skipped(self, probe):
        found = probe("def broken(:\n    pass\n")
        assert found
        assert found[0][2] == "parse_error"

    def test_task_4_2_literal_case_both_outcomes_in_one_module(self, probe):
        """Task 4.2 verbatim: one temp module holding a valid and a fabricated token.

        Written as its own test because tasks.md names these two tokens
        specifically, and a reader checking the spec against the code should find
        them here rather than having to infer that other contract members were
        used as equivalent samples. The point of pairing them in ONE module is
        that it proves the guard discriminates per token: flagging the file
        wholesale would also "pass" a broken guard that reports everything.
        """
        found = probe(
            'CODE = "POLICY_VIOLATION"\n'        # a real contract member: must pass
            'OTHER = "PLAN_DIGEST_MISMATCHED"\n'  # fabricated near-miss: must fail
        )
        flagged = [entry[1] for entry in found]
        assert "PLAN_DIGEST_MISMATCHED" in flagged, \
            "the fabricated token was not caught"
        assert "POLICY_VIOLATION" not in flagged, \
            "a real contract member was flagged — the guard over-reports"
        assert len(flagged) == 1, \
            f"expected exactly one flag, got {flagged}"


class TestGuardDoesNotOverReport:
    """Precision matters as much as recall: a guard that flags everything gets ignored."""

    @pytest.mark.parametrize("token", [
        "PLAN_DIGEST_MISMATCH", "IDEMPOTENCY_CONFLICT", "SEARCH_ESCALATION_EXHAUSTED",
        "AWAITING_APPROVAL", "BLOCK_AND_LOG", "URGENT_PRIORITY", "HEURISTIC_FALLBACK",
    ])
    def test_real_vocabulary_members_pass(self, probe, token):
        assert not probe(f'X = "{token}"\n'), f"{token} is a contract member and was flagged"

    @pytest.mark.parametrize("token", ["HC-001", "HC-013", "EVAL-001", "EVAL-030", "EVT-006", "EVT-007"])
    def test_real_prefixed_ids_pass(self, probe, token):
        assert not probe(f'X = "{token}"\n'), f"{token} exists in the contract and was flagged"

    def test_docstring_prose_is_not_scanned(self, probe):
        src = '''
            """This docstring mentions PLAN_DIGEST_MISMATCHED and HC-099 in prose."""
            X = "PLAN_DIGEST_MISMATCH"
        '''
        assert not probe(src), "docstring prose was scanned; it should be excluded"

    def test_all_exports_are_not_scanned(self, probe):
        assert not probe('__all__ = ["SOME_LOCAL_CONSTANT", "ANOTHER_ONE"]\n')

    def test_dotted_references_in_messages_are_not_scanned(self, probe):
        src = 'raise RuntimeError("see planpilot.NOT_A_REAL_MEMBER for details")\n'
        assert not probe(src), "a dotted attribute reference was treated as a vocabulary claim"

    @pytest.mark.parametrize("filename", [
        "IMPLEMENTATION_NOTES.md",
        "REVIEW_HANDOFF_FOR_CODEX.md",
        "REVIEW_HANDOFF_IMPLEMENTATION.md",
        "V1.8_changelog.md",
    ])
    def test_filename_references_are_not_scanned(self, probe, filename):
        """A token followed by `.ext` names a file; it is not a vocabulary claim.

        Pinned because this exclusion was added after the guard flagged six
        filename stems in tools/factcheck_impl_handoff.py, which blocked the whole
        test suite via test_the_repository_passes_its_own_guard. Without this
        test someone could "simplify" the lookahead away and reintroduce it.
        """
        assert not probe(f'p = ROOT / "{filename}"\n')

    def test_a_trailing_sentence_period_does_not_disable_detection(self, probe):
        """The lookahead must not swallow a real violation ending a sentence."""
        found = probe('msg = "the code was PLAN_DIGEST_MISMATCHED."\n')
        assert found, "a fabricated token before a sentence period was not flagged"
        assert found[0][1] == "PLAN_DIGEST_MISMATCHED"

    def test_implementation_namespace_is_not_scanned(self, probe):
        assert not probe('X = "PLANPILOT_SOMETHING_NEW"\n')

    def test_lowercase_and_mixed_case_are_not_scanned(self, probe):
        assert not probe('a = "some_local_value"\nb = "DigestMismatchError"\n')

    def test_defined_python_constants_are_not_scanned(self, probe, guard):
        """CONTRACT_SHA256 is an implementation constant, not a vocabulary claim."""
        src = 'CONTRACT_SHA256 = "abc"\nX = "CONTRACT_SHA256"\n'
        guard._DEFINED_NAMES = guard.collect_defined_names([]) | {"CONTRACT_SHA256"}
        try:
            assert not probe(src)
        finally:
            guard._DEFINED_NAMES = set()


class TestVocabulariesAreComplete:
    """The guard is only as good as the vocabularies it collected."""

    def test_error_codes_match_the_contract(self, guard, fixtures):
        registry = fixtures.contract()["$defs"]["tool_error"]["properties"]["error_code"]["enum"]
        assert guard.VOCABULARIES["error_code"] == set(registry)
        assert len(registry) == 18

    def test_validation_codes_match_the_contract(self, guard, fixtures):
        enum = fixtures.contract()["$defs"]["validation_issue_code"]["enum"]
        assert guard.VOCABULARIES["validation_issue_code"] == set(enum)
        assert len(enum) == 36

    def test_hard_constraints_are_collected(self, guard, fixtures):
        ids = {h["id"] for h in fixtures.contract()["hard_constraints"]}
        assert guard.PREFIXED_VALID["hard_constraint"] == ids
        assert len(ids) == 13

    def test_eval_cases_use_case_id_not_id(self, guard, fixtures):
        """Regression: the first draft read t["id"] and raised KeyError."""
        ids = {t["case_id"] for t in fixtures.contract()["acceptance_tests"]}
        assert guard.PREFIXED_VALID["eval_case"] == ids
        assert len(ids) == 30

    def test_events_are_collected_from_disruption_coverage(self, guard, fixtures):
        coverage = fixtures.contract()["data_source"]["official_disruption_coverage"]
        ids = {e for group in coverage.values() for e in group}
        assert guard.PREFIXED_VALID["event"] == ids
        assert ids == {"EVT-001", "EVT-002", "EVT-003", "EVT-004", "EVT-005", "EVT-006", "EVT-007"}

    def test_security_actions_include_block_and_log(self, guard):
        """BLOCK_AND_LOG lives in security_controls, not approval_rules."""
        assert "BLOCK_AND_LOG" in guard.VOCABULARIES["security_action"]
        assert "BLOCK_AND_LOG" in guard.ALL_KNOWN

    def test_approval_decisions_are_collected(self, guard, fixtures):
        decisions = {r["decision"] for r in fixtures.contract()["approval_rules"]}
        assert guard.VOCABULARIES["approval_decision"] == decisions

    def test_no_vocabulary_is_empty(self, guard):
        empty = [k for k, v in guard.VOCABULARIES.items() if not v]
        assert not empty, f"empty vocabularies: {empty}"

    def test_every_vocabulary_is_a_subset_of_all_known(self, guard):
        for name, members in guard.VOCABULARIES.items():
            assert members <= guard.ALL_KNOWN, f"{name} is not merged into ALL_KNOWN"


class TestAllowlistHygiene:
    def test_no_allowlist_entry_is_a_contract_member(self, guard):
        """A stale entry would hide a real vocabulary value from review."""
        stale = {t for t in guard.ALLOWED_LOCAL if t in guard.ALL_KNOWN}
        assert not stale, f"allowlist hides contract members: {stale}"

    def test_every_allowlist_entry_declares_paths(self, guard):
        for token, (reason, paths) in guard.ALLOWED_LOCAL.items():
            assert reason.strip(), f"{token} has no reason"
            assert paths, f"{token} has no path scope"

    def test_allowlist_is_path_scoped_not_global(self, guard):
        """The whole point: a token allowed in a fixture file is still caught in src/.

        Asserts the real invariant rather than a hardcoded filename: every entry is
        scoped to at least one fixture file under tests/, and classify() — which
        knows nothing about paths — still flags the token. If classify() ever
        returned None for an allowlisted token, the allowlist would have leaked into
        a global exemption.
        """
        for token, (_reason, paths) in guard.ALLOWED_LOCAL.items():
            assert paths, f"{token} has no path scope"
            # tests/ fixtures are the classic scope; evidence checkers under
            # tools/factcheck_* assert status literals and are auditable by
            # filename. Anything else (src/, general tools/) would be a
            # production-code exemption defeating the guard.
            assert all(p.startswith(("tests/", "tools/factcheck_")) for p in paths), (
                f"{token} is scoped outside tests/ and factcheck tools ({paths}); "
                f"a production-code exemption would defeat the guard"
            )
            assert guard.classify(token) is not None, f"{token} is globally exempted"

    def test_every_allowlisted_token_appears_in_a_file_it_is_scoped_to(self, guard):
        """Guards against the allowlist rotting into dead entries again.

        Each token must actually appear in at least one of the files its entry
        names — otherwise the entry can never fire (the D3 defect class).
        """
        for token, (_reason, paths) in guard.ALLOWED_LOCAL.items():
            found_in = [p for p in paths if (guard.ROOT / p).exists()
                        and token in (guard.ROOT / p).read_text(encoding="utf-8")]
            assert found_in, (
                f"{token} is allowlisted for {paths} but appears in none of them"
            )

    def test_negative_fixture_paths_do_not_include_the_fixture_files(self, guard):
        """The fixture modules must stay SCANNED, or the allowlist is never exercised."""
        for rel in (
            Path(__file__).resolve().relative_to(guard.ROOT).as_posix(),
            "tests/unit/test_closed_vocabularies.py",
            "tests/unit/test_guard_bypass_regressions.py",
        ):
            assert not any(rel == p or rel.startswith(p) for p in guard.NEGATIVE_FIXTURE_PATHS), (
                f"{rel} is skipped entirely, so its allowlist entries are never tested"
            )


class TestGuardRunsCleanOnTheRepository:
    def test_the_repository_passes_its_own_guard(self, guard):
        """End-to-end: running the guard over the real tree must exit 0."""
        rc = guard.main([])
        assert rc == 0, "tools/check_closed_vocabularies.py reports violations in the repo"

    def test_self_test_passes(self, guard, tmp_path):
        rc = guard._self_test(tmp_path)
        assert rc == 0
