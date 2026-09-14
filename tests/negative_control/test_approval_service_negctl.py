"""Mutation control for the approval-service contract boundary.

Every mutation is applied only to a disposable repository copy.  A control is
counted as caught only when the real approval unit suite fails; a missing anchor
is a hard failure rather than a silent pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TARGET = Path("src/planpilot/approval/service.py")
TEST = Path("tests/unit/test_approval_service.py")


def _once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise AssertionError(f"[{label}] anchor occurs {count} times, expected 1")
    return source.replace(old, new)


MUTATIONS = [
    (
        "tool output schema bypassed",
        '        validate_tool_payload(\n            validation_result,',
        '        (lambda *args, **kwargs: None)(\n            validation_result,',
    ),
    (
        "validator KPIs no longer bound to immutable content",
        '        if canonical_json(validation_result["kpis"]) != canonical_json(content["kpis"]):',
        "        if False:  # MUTATION: KPI binding bypassed",
    ),
    (
        "approver role mapping ignored",
        '            if requirement["approver_role"] != ROLE_BY_ACTION[requirement["action"]]:',
        "            if False:  # MUTATION: role mapping ignored",
    ),
    (
        "overtime requirement no longer derived from KPI",
        '        if ("add_overtime" in required_actions) != overtime_required:',
        "        if False:  # MUTATION: overtime policy ignored",
    ),
    (
        "impact set may omit required action",
        "        if not isinstance(impact_by_action, dict) or set(impact_by_action) != set(\n            required_actions\n        ):",
        "        if False:  # MUTATION: incomplete impact set accepted",
    ),
    (
        "trigger narrows the complete approval set",
        "        required_actions = ordered_actions(required_actions)\n        set_id = _stable_id",
        "        required_actions = [action]  # MUTATION: trigger narrows set\n        set_id = _stable_id",
    ),
    (
        "minimum approval TTL not enforced",
        "            effective = min(max(candidate, lower), upper)",
        "            effective = min(candidate, upper)  # MUTATION: minimum TTL removed",
    ),
    (
        "authenticated role check bypassed",
        '        if actor_role != approval["approver_role"]:',
        "        if False:  # MUTATION: authenticated role ignored",
    ),
    (
        "rejected no longer wins aggregate precedence",
        '        if "REJECTED" in statuses:',
        "        if False:  # MUTATION: rejection precedence removed",
    ),
    (
        "new set remains in memory after failed persist",
        "        except BaseException:\n            del self._sets[set_id]\n            del self._set_by_binding[key]\n            raise",
        "        except BaseException:\n            raise  # MUTATION: creation rollback removed",
    ),
    (
        "loaded validation evidence bypasses semantic validation",
        "            normalized = self._validate_loaded_validation_record(record)",
        "            normalized = copy.deepcopy(record)  # MUTATION: load validation bypassed",
    ),
    (
        "loaded set no longer matches validated membership and impact",
        "            self._assert_matches_validation(record, validated[key])",
        "            pass  # MUTATION: validated evidence binding bypassed",
    ),
    (
        "supersede event acknowledged before durable invalidation",
        "                self.invalidate_set(\n                    set_id, event[\"invalidation_cause\"], event[\"plan_digest\"]\n                )",
        "                pass  # MUTATION: invalidation omitted before acknowledgement",
    ),
    (
        "null lifecycle set id no longer falls back to approval binding",
        "            set_id = event_set_id or bound_set_id",
        "            set_id = event_set_id  # MUTATION: binding fallback removed",
    ),
]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp("approval-negctl") / "repo"
    target.mkdir()
    for source in ROOT.iterdir():
        if source.name in {".git", ".pytest_cache", ".venv-review"}:
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


def _run(root: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(root / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", str(TEST)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=env,
    )


def test_approval_mutations_are_detected(sandbox):
    source_path = sandbox / TARGET
    pristine = source_path.read_bytes()
    working_before = (ROOT / TARGET).read_bytes()
    baseline = _run(sandbox)
    assert baseline.returncode == 0, baseline.stdout[-3000:]

    caught = []
    broken = []
    escaped = []
    try:
        original = pristine.decode("utf-8")
        for label, old, new in MUTATIONS:
            try:
                mutated = _once(original, old, new, label)
            except AssertionError as exc:
                broken.append(str(exc))
                continue
            source_path.write_bytes(mutated.encode("utf-8"))
            try:
                result = _run(sandbox)
            finally:
                source_path.write_bytes(pristine)
            if result.returncode:
                caught.append(label)
            else:
                escaped.append(label)
    finally:
        source_path.write_bytes(pristine)

    print(
        f"APPROVAL NEGATIVE CONTROL | caught={len(caught)} "
        f"escaped={len(escaped)} broken={len(broken)} of {len(MUTATIONS)}"
    )
    assert source_path.read_bytes() == pristine
    assert (ROOT / TARGET).read_bytes() == working_before
    assert not broken, broken
    assert not escaped, escaped
    assert len(caught) == len(MUTATIONS)
