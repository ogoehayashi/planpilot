"""Task 5.1 — negative control for the plan-store-and-digest spec.

A test suite that cannot fail is not a test suite. This project has already
shipped an assertion containing `or True`, and a self-check that counted the
guard's own body as evidence of use. Both looked green.

Method — deliberately the unambiguous one
-----------------------------------------
The repository is copied to a pytest temporary directory once per module. Each
mutation is written only into that disposable copy, whose real pytest suite is
then run as a subprocess. A mutation counts as CAUGHT only if that suite FAILS.
The sandbox file is restored in a `finally` so sequential mutations stay
independent; if pytest itself is killed, only the temporary copy can remain
dirty. The working repository is never a mutation target.

The first draft of this file did the opposite: it imported the mutated module and
asked "did the protection hold?", then recorded True as "caught". That polarity
inversion reported 10 caught / 5 escaped when the truth was close to the reverse.
Running the actual suite removes the ambiguity entirely — there is nothing to
interpret.

It also caught a second, subtler problem: flipping `allow_nan=False` to `True`
looks like it defeats the non-finite guard, but `_reject_non_finite()` runs first
and still blocks it. A mutation that does not reach the behaviour it claims to
test is worthless. So the float defence is probed three ways (layer one only,
layer two only, both removed), which proves the defence in depth is real rather
than accidental.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTEST = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]

_SANDBOX_EXCLUDES = {".git", ".pytest_cache", ".venv-review"}


@pytest.fixture(scope="module")
def sandbox_root(tmp_path_factory) -> Path:
    """Make a disposable but faithful checkout for destructive mutation runs.

    Copying all project entries except VCS/cache/venv state keeps path-sensitive
    unit tests and the vocabulary guard honest. The copy lives outside ROOT, so
    SIGKILL after a mutation write cannot alter a file that can be committed.
    """
    target = tmp_path_factory.mktemp("planpilot-negctl") / "repo"
    target.mkdir()
    for source in ROOT.iterdir():
        if source.name in _SANDBOX_EXCLUDES:
            continue
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        else:
            shutil.copy2(source, destination)
    return target


def _sub_once(source: str, old: str, new: str, label: str) -> str:
    """Replace exactly one occurrence, or fail loudly.

    A mutation whose pattern no longer matches silently becomes a no-op, and a
    no-op mutation always "passes" — which is how a negative control turns into
    theatre. Asserting the count is what keeps it honest.
    """
    n = source.count(old)
    if n != 1:
        raise AssertionError(f"[{label}] pattern occurs {n} times, expected 1:\n{old!r}")
    return source.replace(old, new)


# (label, file, mutation, suite that must fail)
MUTATIONS: list[tuple[str, str, object, str]] = [
    # ---- canonical form
    (
        "canonical_json loses sort_keys",
        "digest.py",
        lambda s: _sub_once(s, "sort_keys=True", "sort_keys=False", "sort_keys"),
        "test_digest_determinism.py",
    ),
    (
        "canonical_json adds whitespace",
        "digest.py",
        lambda s: _sub_once(s, 'separators=(",", ":")', 'separators=(", ", ": ")', "separators"),
        "test_digest_determinism.py",
    ),
    (
        "canonical_json escapes non-ASCII",
        "digest.py",
        lambda s: _sub_once(
            s,
            "        ensure_ascii=False,",
            "        ensure_ascii=True,  # MUTATION: non-ASCII escaped",
            "ensure_ascii",
        ),
        "test_digest_determinism.py",
    ),
    (
        "operations not sorted before hashing",
        "digest.py",
        lambda s: _sub_once(
            s,
            '        payload["operations"] = sort_operations(ops)',
            "        pass  # MUTATION: sorting removed",
            "sort_operations call",
        ),
        "test_digest_determinism.py",
    ),
    (
        "plan_digest no longer excluded (circularity returns)",
        "digest.py",
        lambda s: _sub_once(
            s,
            'DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"plan_digest"})',
            "DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset()",
            "DIGEST_EXCLUDED_FIELDS",
        ),
        "test_digest_determinism.py",
    ),
    (
        "engine.canonical_plan_hash no longer excluded",
        "digest.py",
        lambda s: _sub_once(
            s,
            'ENGINE_DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"canonical_plan_hash"})',
            "ENGINE_DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset()",
            "ENGINE_DIGEST_EXCLUDED_FIELDS",
        ),
        "test_digest_determinism.py",
    ),
    # ---- the two digest identities
    (
        "second identity (canonical_plan_hash) not checked",
        "digest.py",
        lambda s: _sub_once(
            s,
            "    if canonical_hash != recomputed:",
            "    if False:  # MUTATION: second identity unchecked",
            "second identity check",
        ),
        "test_digest_identity.py",
    ),
    (
        "first identity (plan_digest) not checked",
        "digest.py",
        lambda s: _sub_once(
            s,
            "    if declared != recomputed:",
            "    if False:  # MUTATION: first identity unchecked",
            "first identity check",
        ),
        "test_digest_identity.py",
    ),
    # ---- the float defence.
    #
    # Measured, not assumed. The two layers are NOT equivalent, and the first
    # draft of this file wrongly expected removing layer 1 to be harmless:
    #
    #   layer 1 `_reject_non_finite` raises CanonicalizationError carrying
    #           contract-shaped details (validates against
    #           $defs.error_details_invalid_input) plus the json_path
    #   layer 2 `allow_nan=False`   raises a bare ValueError
    #           ("Out of range float values are not JSON compliant") with no
    #           details and no path
    #
    # So removing layer 1 still blocks NaN — but reports it in a way the tool
    # layer cannot turn into a valid tool_error. That is a real regression, and
    # the suite correctly fails. Layer 2 is a backstop against emitting invalid
    # JSON, not a substitute for layer 1.
    (
        "float layer 1 removed — suite MUST fail (error loses its contract shape)",
        "digest.py",
        lambda s: _sub_once(
            s,
            "    _reject_non_finite(obj, _path, _entity_id)",
            "    pass  # MUTATION: explicit non-finite scan removed",
            "_reject_non_finite call",
        ),
        "test_digest_determinism.py",
    ),
    (
        "float layer 2 removed (allow_nan) — layer 1 must still hold",
        "digest.py",
        lambda s: _sub_once(s, "allow_nan=False", "allow_nan=True", "allow_nan"),
        # Deterministic hold suite (p2-7 reviewer: defence-in-depth checks
        # must not be exposed to the full-suite HTTP flake): the two files
        # that exercise non-finite floats through layer 1 — canonicalization
        # and the tool-error contract shape. Neither opens a socket
        # (asserted structurally by test_defence_suites_are_http_free).
        ("test_digest_determinism.py", "test_tool_error_middleware.py"),
    ),
    (
        "float defence removed entirely — suite MUST fail",
        "digest.py",
        lambda s: _sub_once(
            _sub_once(s, "allow_nan=False", "allow_nan=True", "allow_nan (both)"),
            "    _reject_non_finite(obj, _path, _entity_id)",
            "    pass  # MUTATION: both float layers removed",
            "_reject_non_finite (both)",
        ),
        "test_digest_determinism.py",
    ),
    (
        "isolated-surrogate guard removed",
        "digest.py",
        lambda s: _sub_once(
            s,
            "            if 0xD800 <= ord(char) <= 0xDFFF:",
            "            if False:  # MUTATION: isolated surrogate accepted",
            "surrogate guard",
        ),
        "test_digest_determinism.py",
    ),
    (
        "negative zero normalization removed",
        "digest.py",
        lambda s: _sub_once(
            s,
            "    normalized = _normalize_negative_zero(obj)",
            "    normalized = obj  # MUTATION: signed zero not normalized",
            "negative-zero normalization",
        ),
        "test_digest_determinism.py",
    ),
    # ---- store invariants
    (
        "write-once enforcement removed",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            raise IdempotencyConflictError(",
            "            return digest  # MUTATION: silent overwrite\n            raise IdempotencyConflictError(",
            "IdempotencyConflictError raise",
        ),
        "test_plan_store_invariants.py",
    ),
    (
        "digest not verified on write",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "        digest = assert_digest_consistent(content)",
            '        digest = content.get("plan_digest")  # MUTATION: trust the caller',
            "assert_digest_consistent call",
        ),
        "test_plan_store_invariants.py",
    ),
    (
        "get_content hands out the internal dict",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "        return copy.deepcopy(self._content[(plan_id, version)])",
            "        return self._content[(plan_id, version)]  # MUTATION: no copy",
            "get_content deepcopy",
        ),
        "test_plan_store_invariants.py",
    ),
    (
        "supersede made idempotent (double event possible)",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '        if record["status"] in _TERMINAL:',
            '        if False:  # MUTATION: re-supersede allowed',
            "terminal supersede guard",
        ),
        "test_plan_store_invariants.py",
    ),
    (
        "re-creating a lifecycle record allowed",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            raise LifecycleAlreadyExistsError(",
            "            return copy.deepcopy(self._lifecycle[key])  # MUTATION\n            raise LifecycleAlreadyExistsError(",
            "LifecycleAlreadyExistsError raise",
        ),
        "test_plan_store_invariants.py",
    ),
    (
        "optimistic concurrency check removed",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "        if expected_plan_version is not None and expected_plan_version != active:",
            "        if False:  # MUTATION: OCC disabled",
            "expected_plan_version check",
        ),
        "test_plan_store_invariants.py",
    ),
    (
        "P0-2 regression: OCC compares the caller's own argument again (tautology)",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "        if expected_plan_version is not None and expected_plan_version != active:",
            "        if expected_plan_version is not None and expected_plan_version != version:  # MUTATION: P0-2 tautology",
            "expected_plan_version != active",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "P0-2 authority gate removed (stale version can be published)",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "        if new_status in _ACTIVE_ONLY_STATUSES and version != active:",
            "        if False:  # MUTATION: authority gate removed",
            "authority-status gate",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "P0-1 schema gate removed from put_content",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '        self._validate_record(content, "plan_content", plan_id)',
            "        pass  # MUTATION: schema gate removed",
            "put_content schema gate",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "P0-1 lifecycle validation removed from create_lifecycle",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '        self._validate_record(record, "plan_lifecycle", plan_id)',
            "        pass  # MUTATION: lifecycle not validated",
            "create_lifecycle schema gate",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "P0-1 transition no longer validates the candidate lifecycle",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '        self._validate_record(candidate, "plan_lifecycle", plan_id)',
            "        pass  # MUTATION: candidate not validated",
            "transition schema gate",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "P1 duplicate-key check removed from load_state content",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            if key in content_by_key:",
            "            if False:  # MUTATION: duplicate content key ignored",
            "load_state content duplicate check",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "P1 supersede-event existence check removed from load_state",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            if key not in content_by_key:\n                raise PlanNotFoundError(\n                    str(event[\"plan_id\"]), event[\"plan_version\"],\n                    lookup_kind=\"plan_content\",\n                )",
            "            if False:  # MUTATION: fabricated events accepted\n                raise PlanNotFoundError(\n                    str(event[\"plan_id\"]), event[\"plan_version\"],\n                    lookup_kind=\"plan_content\",\n                )",
            "load_state event existence check",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "commit_new_version no longer supersedes the old version",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            if previous is not None and (plan_id, previous) in self._lifecycle:\n                self.supersede(plan_id, previous, ts)",
            "            if False:  # MUTATION: old version left live\n                self.supersede(plan_id, previous, ts)",
            "commit_new_version supersede",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "commit_new_version continuity check removed",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            if previous is not None and plan_version != previous + 1:",
            "            if False:  # MUTATION: any version number accepted",
            "commit_new_version continuity",
        ),
        "test_p0_audit_regressions.py",
    ),
    (
        "load_state trusts the dump",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            assert_digest_consistent(record)",
            "            pass  # MUTATION: dumps are trusted",
            "load_state verify",
        ),
        "test_plan_store_persistence.py",
    ),
    (
        "dump uses non-canonical JSON",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "        text = canonical_json(state)",
            "        text = json.dumps(state, indent=2)  # MUTATION: not canonical",
            "dump canonical_json",
        ),
        "test_plan_store_persistence.py",
    ),
    # ---- the two defect classes task 5.1 names that were missing.
    #
    # Found while auditing tasks.md line by line: all 28 mutations above covered
    # floats, canonicalisation, digest identities, write-once, supersede, the P0
    # gates and load_state — but NOT "clock read internally" and NOT "fabricated
    # code accepted". Both are named in task 5.1, and both are core guarantees of
    # this module (injected clock = reproducible evidence pack; no invented code =
    # every tool_error is emittable). A gap here means a regression in either
    # would ship green.
    (
        "clock read internally instead of injected via ts",
        "plan_store.py",
        lambda s: _sub_once(
            _sub_once(
                s,
                "from pathlib import Path",
                "import datetime  # MUTATION\nfrom pathlib import Path",
                "datetime import",
            ),
            '            "updated_at": ts,',
            '            "updated_at": datetime.datetime.now().isoformat(),  # MUTATION: clock read internally',
            "updated_at from the ts argument",
        ),
        "test_plan_store_invariants.py",
    ),
    (
        "fabricated error code accepted",
        "errors.py",
        lambda s: _sub_once(
            s,
            '    code = "PLAN_DIGEST_MISMATCH"',
            '    code = "PLAN_DIGEST_MISMATCHED"  # MUTATION: not a registered code',
            "DigestMismatchError.code",
        ),
        "test_errors_schema.py",
    ),
    # ---- third audit: P0-bis, P1-a, P1-b, P2, and the two defects found fixing
    # them. One mutation per defence, so removing any single one fails the suite.
    #
    # P0-bis: creation may not mint authority. The defence is the constant, so the
    # mutation substitutes an authority status for it.
    (
        "create_lifecycle mints PUBLISHED instead of DRAFT",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            "status": CREATION_STATUS,',
            '            "status": "PUBLISHED",  # MUTATION: authority at creation',
            "CREATION_STATUS in the lifecycle record",
        ),
        "test_plan_store_invariants.py",
    ),
    # P1-b: put_content may not add a version to an existing plan.
    (
        "put_content route gate removed",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "        if not allow_new_version:",
            "        if False:  # MUTATION: any caller may add a version",
            "allow_new_version gate",
        ),
        "test_audit3_regressions.py",
    ),
    # P1-b second half: a dump may not resurrect stale authority.
    (
        "load_state layer-3 stale-authority check removed",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '                if lc is not None and (\n'
            '                    lc["status"] in _ACTIVE_ONLY_STATUSES\n'
            '                    or (lc["approval_set_id"] is not None and lc["status"] != "SUPERSEDED")\n'
            '                ):',
            "                if False:  # MUTATION",
            "layer-3 authority check",
        ),
        "test_audit3_regressions.py",
    ),
    # P1-a: the five event<->lifecycle relationships.
    (
        "P1-a(1): event no longer needs a lifecycle record",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            if lc is None:",
            "            if False:  # MUTATION",
            "event needs lifecycle",
        ),
        "test_audit3_regressions.py",
    ),
    (
        "P1-a(3): event approval_set_id no longer compared",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            if event["approval_set_id"] != lc["approval_set_id"]:',
            "            if False:  # MUTATION",
            "approval_set_id agreement",
        ),
        "test_audit3_regressions.py",
    ),
    (
        "P1-a(4): duplicate supersede events allowed",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            if event_count[key] > 1:",
            "            if False:  # MUTATION: duplicate history allowed",
            "unique supersede history events",
        ),
        "test_audit3_regressions.py",
    ),
    (
        "P1-a(5): superseded record no longer needs an event",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "            if event_count.get(key, 0) != 1:",
            "            if False:  # MUTATION: approvals never invalidated",
            "converse event check",
        ),
        "test_audit3_regressions.py",
    ),
    # P1-a(2) is covered TWICE: an event whose record is not SUPERSEDED is also a
    # stale version holding authority, so layer 3 refuses it independently. Both
    # were measured, so this asserts the redundancy rather than a single catch.
    (
        "P1-a(2): event no longer requires status SUPERSEDED",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            if lc["status"] != "SUPERSEDED":',
            "            if False:  # MUTATION",
            "event requires SUPERSEDED",
        ),
        # Deterministic hold suite (p2-7 reviewer, same rule as the float
        # layer-2 case): layer 3's independent catch lives in the audit-3
        # regressions (stale-version-holds-authority on the load path).
        # Socket-free, so this verdict cannot ride on HTTP timing.
        ("test_audit3_regressions.py",),
    ),
    # Incidental: dump_state must sort superseded_events, or the evidence hash
    # depends on insertion order (F4/F12 class).
    (
        "dump_state emits superseded_events in insertion order",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            "superseded_events": sorted(self._superseded, key=_event_sort_key),',
            '            "superseded_events": list(self._superseded),  # MUTATION',
            "event sort in dump_state",
        ),
        "test_audit3_regressions.py",
    ),
    # P2: semantic selection, pinned hash, verified override.
    (
        "contract selection reverts to lexicographic",
        "src/planpilot/validation/schema.py",
        lambda s: _sub_once(
            s,
            "    chosen = max(matches, key=lambda p: _contract_version(p.name))",
            "    chosen = sorted(matches)[-1]  # MUTATION: v1.9 beats v1.10",
            "semantic max",
        ),
        "test_audit3_regressions.py",
    ),
    (
        "contract hash pin not enforced",
        "src/planpilot/validation/schema.py",
        lambda s: _sub_once(
            s,
            "    if actual != CONTRACT_SHA256:",
            "    if False:  # MUTATION: any contract on disk is trusted",
            "CONTRACT_SHA256 comparison",
        ),
        "test_audit3_regressions.py",
    ),
    (
        "contract override accepted without a digest",
        "src/planpilot/validation/schema.py",
        lambda s: _sub_once(
            s,
            "        if not expected:",
            "        if False:  # MUTATION: bare PLANPILOT_CONTRACT_PATH allowed",
            "override requires digest",
        ),
        "test_audit3_regressions.py",
    ),
    (
        "contract override digest not compared",
        "src/planpilot/validation/schema.py",
        lambda s: _sub_once(
            s,
            "        if actual != expected:",
            "        if False:  # MUTATION: a tampered contract is trusted",
            "override digest comparison",
        ),
        "test_audit3_regressions.py",
    ),
    # Single-compiler guarantee: the fixtures must not carry a second validator.
    # Reintroducing one is the mutation; both the identity test and the
    # "no _SCOPED/_VALIDATOR" test catch it.
    (
        "tests/_fixtures.py grows a second schema compiler",
        "tests/_fixtures.py",
        lambda s: _sub_once(
            s,
            'def _validator_for(def_name: str):\n'
            '    """Validator scoped to one $defs entry — the production one, not a copy."""\n'
            '    return _production_validator_for(def_name)',
            '_SCOPED = {}  # MUTATION: a second compiler is back\n'
            '\n'
            '\n'
            'def _validator_for(def_name: str):\n'
            '    """MUTATION: compiles its own validator."""\n'
            '    if def_name not in _SCOPED:\n'
            '        import jsonschema\n'
            '        _SCOPED[def_name] = jsonschema.Draft202012Validator(\n'
            '            {"$defs": _CONTRACT["$defs"]},\n'
            '            format_checker=jsonschema.FormatChecker(),\n'
            '        ).evolve(schema={"$ref": f"#/$defs/{def_name}"})\n'
            '    return _SCOPED[def_name]',
            "_validator_for delegation",
        ),
        "test_audit3_regressions.py",
    ),
    # ---- fourth audit: durable outbox + semantic lifecycle relationships
    (
        "stale version may enter AWAITING_APPROVAL",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '_ACTIVE_ONLY_STATUSES = frozenset({"AWAITING_APPROVAL", "APPROVED", "PUBLISHED"})',
            '_ACTIVE_ONLY_STATUSES = frozenset({"APPROVED", "PUBLISHED"})  # MUTATION',
            "active-only approval request",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "DRAFT may jump directly to PUBLISHED",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '                record["status"] != "APPROVED"\n'
            '                or candidate["approval_set_id"] is None',
            '                False  # MUTATION: predecessor ignored\n'
            '                or candidate["approval_set_id"] is None',
            "published predecessor",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "published_version need not equal plan_version",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '                or candidate["published_version"] != version',
            '                or False  # MUTATION: publication binding ignored',
            "published version binding",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "approval-set binding may be replaced",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            if record["approval_set_id"] not in (None, approval_set_id):',
            '            if False:  # MUTATION: approval binding can be replaced',
            "approval binding immutability",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "PUBLISHED may return to a mutable state",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '        if record["status"] == "PUBLISHED" and new_status != "SUPERSEDED":',
            '        if False:  # MUTATION: published rollback accepted',
            "published successor restriction",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "public transition may bypass atomic supersede event creation",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '        if new_status == "SUPERSEDED" and not allow_superseded:',
            '        if False:  # MUTATION: direct SUPERSEDED accepted',
            "public SUPERSEDED route gate",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "supersede event cause is not bound to its type",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            if event["invalidation_cause"] != _CAUSE_SUPERSEDED:',
            '            if False:  # MUTATION: cause relationship ignored',
            "supersede cause",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "supersede event time no longer matches lifecycle",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            if event["superseded_at"] != lc["updated_at"]:',
            '            if False:  # MUTATION: time relationship ignored',
            "supersede time",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "load_state accepts a version gap",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            "                if current != previous + 1:",
            "                if False:  # MUTATION: continuity ignored",
            "load-state continuity",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "acknowledgement is not persisted",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            "superseded_acks": sorted(self._superseded_acks.values(), key=_ack_sort_key),',
            '            "superseded_acks": [],  # MUTATION: ack lost on restart',
            "ack persistence",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "acknowledged event is still returned as pending",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '            if (event["plan_id"], event["plan_version"]) not in self._superseded_acks',
            '            if True  # MUTATION: ack ignored',
            "pending filter",
        ),
        "test_audit4_regressions.py",
    ),
    (
        "acknowledgement binding is not verified",
        "plan_store.py",
        lambda s: _sub_once(
            s,
            '        if (\n'
            '            event["plan_digest"] != plan_digest\n'
            '            or event["approval_set_id"] != approval_set_id\n'
            '        ):',
            '        if False:  # MUTATION: acknowledgement binding ignored',
            "ack binding",
        ),
        "test_audit4_regressions.py",
    ),
]


def _run_suite(root: Path, test_file) -> subprocess.CompletedProcess:
    unit_tests = root / "tests" / "unit"
    if test_file is None:
        targets = [unit_tests]
    elif isinstance(test_file, str):
        targets = [unit_tests / test_file]
    else:
        targets = [unit_tests / f for f in test_file]
    # PYTHONDONTWRITEBYTECODE is load-bearing, not a tidy-up. CPython validates a
    # cached .pyc against the source mtime TRUNCATED TO WHOLE SECONDS plus its
    # size, so when two mutations are written within the same second and happen to
    # produce the same byte length, the child process imports the PREVIOUS
    # mutation's stale bytecode and runs the wrong code. That made this control
    # intermittent: P1-a(2) — an equal-length `if …:` -> `if False:` swap —
    # reported a false escape inside the control while passing 7/7 in isolation.
    # Reproduced deterministically in tools/probes/probe_pyc_stale.py.
    #
    # Disabling bytecode writing means every child compiles from source, so a
    # mutation can never be masked by a stale .pyc. The cost is recompiling src/
    # per child, which is a few files.
    inherited_path = os.environ.get("PYTHONPATH")
    sandbox_path = str(root / "src")
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": sandbox_path + (os.pathsep + inherited_path if inherited_path else ""),
    }
    return subprocess.run(
        [*PYTEST, *[str(t) for t in targets]],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600, env=env,
    )


def _clear_pycache(root: Path) -> int:
    """Remove every __pycache__ under src/ and tests/ so no stale .pyc survives.

    The sandbox copy already excludes caches. Clearing once before the mutation
    loop, combined with PYTHONDONTWRITEBYTECODE on every child, also protects
    against any bytecode produced by the baseline or a future sandbox setup step.
    Only the sandbox's src/ and tests/ are touched — never the working tree or venv.
    """
    removed = 0
    for base in (root / "src", root / "tests"):
        for cache in base.rglob("__pycache__"):
            if cache.is_dir():
                shutil.rmtree(cache, ignore_errors=True)
                removed += 1
    return removed


def _mutate_target(root: Path, name: str) -> Path:
    """Where a mutation's file lives.

    A bare basename means `src/planpilot/store/<name>`, which is how every
    original entry was written. Anything else is ROOT-relative, so a mutation can
    also reach `src/planpilot/validation/schema.py` (audit finding P2) or
    `tests/_fixtures.py` (the second-compiler guarantee) without a second
    hardwired directory.

    Resolved by checking the store directory first rather than guessing from the
    string: a file existing in both places would otherwise be silently mutated in
    the wrong one, and the mutation would report "no change" or — worse — change
    something the suite does not cover.
    """
    store = root / "src" / "planpilot" / "store"
    candidate = store / name
    if candidate.exists():
        return candidate
    resolved = root / name
    if not resolved.exists():
        raise AssertionError(
            f"mutation target {name!r} is neither in {store} nor at {resolved}"
        )
    return resolved


@pytest.fixture(scope="module")
def baseline(sandbox_root):
    """The suite must be green before any mutation, or every result is meaningless."""
    result = _run_suite(sandbox_root, None)
    assert result.returncode == 0, (
        f"baseline suite is not green — negative control cannot interpret results:\n"
        f"{result.stdout[-3000:]}"
    )
    return result


def test_mutations_are_detected(baseline, sandbox_root):
    caught, escaped, broken = [], [], []
    # Defence-in-depth mutations carry an explicit HOLD SUITE (a tuple of
    # socket-free unit files): removing ONE layer must NOT break those tests,
    # because the other layer still holds. The old form of this check ran the
    # ENTIRE tests/unit directory for the hold verdict, which exposed it to
    # the local-HTTP delivery test (urlopen timing under the control's own
    # 58-child load) and produced an unreproducible false escape. A hold
    # verdict must never depend on network timing again — and
    # test_defence_suites_are_http_free keeps that promise structurally.
    still_held = []

    pristine_names = (
        "digest.py", "plan_store.py", "errors.py",
        "src/planpilot/validation/schema.py", "tests/_fixtures.py",
    )
    pristine = {
        name: _mutate_target(sandbox_root, name).read_bytes()
        for name in pristine_names
    }
    working_tree_before = {
        name: _mutate_target(ROOT, name).read_bytes()
        for name in pristine_names
    }

    # Clear any bytecode left by an earlier run BEFORE mutating, so the first
    # child cannot read a .pyc that predates this control. Every child also runs
    # with PYTHONDONTWRITEBYTECODE (see _run_suite), so nothing is rewritten
    # during the loop either. Belt and braces: the env var alone would suffice for
    # children, but a stale .pyc on disk could still be read by any process that
    # does NOT set it, so removing them costs nothing and closes that hole.
    _clear_pycache(sandbox_root)

    try:
        for label, filename, mutate, expected_suite in MUTATIONS:
            path = _mutate_target(sandbox_root, filename)
            original = pristine[filename].decode("utf-8")
            try:
                mutated = mutate(original)
            except AssertionError as exc:
                broken.append(str(exc))
                continue
            if mutated == original:
                broken.append(f"[{label}] mutation produced no change")
                continue

            path.write_bytes(mutated.replace("\r\n", "\n").encode("utf-8"))
            try:
                result = _run_suite(sandbox_root, expected_suite)
            finally:
                path.write_bytes(pristine[filename])

            failed = result.returncode != 0

            if isinstance(expected_suite, tuple):
                # defence in depth: the hold suite must STILL PASS
                if failed:
                    # Record WHICH tests failed, not just that the suite did. This
                    # control once reported an escape that could not be
                    # reproduced by applying the same mutation in isolation (six
                    # runs, all green): the hold verdict had ridden on the full
                    # suite's local-HTTP test. The hold suites are socket-free
                    # now, but the detailed report stays — an unreliable verdict
                    # without failing test ids is worse than none (class of D5).
                    import re as _re
                    failed_ids = _re.findall(r"^FAILED (\S+)", result.stdout, _re.M)
                    err_ids = _re.findall(r"^ERROR (\S+)", result.stdout, _re.M)
                    summary = _re.findall(r"=+ (.*?\d+ (?:failed|error).*?) =+",
                                          result.stdout)
                    detail = (f"; failing tests: {failed_ids or err_ids}"
                              if (failed_ids or err_ids) else
                              f"; no FAILED/ERROR line found, summary={summary[-1:] }")
                    escaped.append(
                        f"{label} (expected the other layer to hold, but the suite "
                        f"failed{detail})")
                else:
                    still_held.append(label)
            else:
                if failed:
                    caught.append(label)
                else:
                    escaped.append(f"{label} (suite passed — the guard did not detect it)")
    finally:
        for name, data in pristine.items():
            _mutate_target(sandbox_root, name).write_bytes(data)

    # ---- sandbox sources are restored; working sources were never targets
    #
    # This used to assert `git status --porcelain src/planpilot/store` was empty.
    # That was WRONG: it couples "did the test undo its own mutations" to "is the
    # working tree committed", so the test fails whenever legitimate work is in
    # progress — twice already, and the message claimed the sources were not
    # restored when in fact they were. The real invariant is byte equality against
    # `pristine`, which is what the finally-block above restores.
    #
    # It now covers files outside store/ as well, since a mutation can reach
    # tests/_fixtures.py and src/planpilot/validation/schema.py — and leaving
    # either mutated would corrupt every later test in the session, not just this
    # one.
    not_restored = [
        name for name, data in pristine.items()
        if _mutate_target(sandbox_root, name).read_bytes() != data
    ]
    restored = not not_restored
    working_tree_changed = [
        name for name, data in working_tree_before.items()
        if _mutate_target(ROOT, name).read_bytes() != data
    ]

    total = len(MUTATIONS)
    print()
    print(f"BROKEN FIXTURES (mutation did not apply): {len(broken)}")
    for b in broken:
        print(f"  {b}")
    print()
    print(f"DEFENCE IN DEPTH — one layer removed, suite still green: {len(still_held)}")
    for s in still_held:
        print(f"  {s}")
    print()
    print(f"NEGATIVE CONTROL (store) | caught={len(caught)} escaped={len(escaped)} "
          f"broken_fixtures={len(broken)} of {total}")
    for e in escaped:
        print(f"  ESCAPED: {e}")
    print(f"sources restored to pre-test bytes: {restored}"
          + (f" (not restored: {not_restored})" if not_restored else ""))
    print(f"working repository unchanged by mutations: {not working_tree_changed}"
          + (f" (changed: {working_tree_changed})" if working_tree_changed else ""))

    assert restored, f"the store sources were not restored: {not_restored}"
    assert not working_tree_changed, (
        f"negative control changed files in the working repository: {working_tree_changed}"
    )
    assert not broken, f"{len(broken)} mutation fixtures did not apply; results would be theatre"
    assert not escaped, f"{len(escaped)} mutations escaped detection"
    # every "must fail" mutation (str suite) caught, plus every defence-in-depth
    # case (tuple hold suite) held
    expected_caught = sum(1 for m in MUTATIONS if isinstance(m[3], str))
    assert len(caught) == expected_caught, f"caught {len(caught)}, expected {expected_caught}"
    expected_held = sum(1 for m in MUTATIONS if isinstance(m[3], tuple))
    assert len(still_held) == expected_held, (
        f"defence-in-depth cases held {len(still_held)}, expected {expected_held}")


def test_defence_suites_are_http_free():
    """Structural guard for the p2-7 reviewer closure.

    Defence-in-depth HOLD suites run inside the mutation loop (up to 58
    sequential children). If any of them opens a local HTTP server, the
    hold verdict rides on socket timing under load — the exact defect that
    once produced an unreproducible false escape via
    test_http_authentication_and_full_publish_flow. So every file named in
    a tuple hold suite is asserted to contain no HTTP/socket machinery at
    all. Adding a flaky file to a hold suite now fails here, in seconds,
    instead of producing a phantom escape an hour later.
    """
    banned = ("urlopen", "http.server", "HTTPServer", "socket", "Request(")
    offenders = {}
    hold_files = {f for m in MUTATIONS if isinstance(m[3], tuple) for f in m[3]}
    assert hold_files, "the defence-in-depth hold suites vanished from MUTATIONS"
    for name in sorted(hold_files):
        path = ROOT / "tests" / "unit" / name
        text = path.read_text(encoding="utf-8")
        hits = [b for b in banned if b in text]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        f"hold-suite files must be socket-free, but these use HTTP: {offenders}"
    )


def test_guard_self_test_still_passes():
    """The closed-vocabulary guard's own self-test must stay green."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_closed_vocabularies.py"), "--self-test"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    assert "SELF-TEST | PASS" in result.stdout


def test_guard_passes_on_the_repository():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_closed_vocabularies.py")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    assert "CLOSED VOCABULARY CHECK | PASS" in result.stdout
