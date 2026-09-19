"""Disposable-copy mutations for P0-6 security, trace and audit controls."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FOCUSED = ("tests/unit/test_security_audit.py", "tests/unit/test_event_workflow.py")
SUBJECTS = (
    "src/planpilot/audit.py", "src/planpilot/persistence.py",
    "src/planpilot/workflow/events.py", "src/planpilot/tools/middleware.py",
    "src/planpilot/factory_state.py",
)


def once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"[{label}] broken anchor: {count} occurrences")
    return text.replace(old, new)


MUTATIONS = (
    ("audit rows become mutable", "src/planpilot/persistence.py",
     "BEFORE UPDATE ON audit_chain BEGIN\n                SELECT RAISE(ABORT, 'audit_chain is append-only');",
     "BEFORE UPDATE ON audit_chain BEGIN\n                SELECT 1; -- MUTATION: audit update accepted"),
    ("audit rows become deletable", "src/planpilot/persistence.py",
     "BEFORE DELETE ON audit_chain BEGIN\n                SELECT RAISE(ABORT, 'audit_chain is append-only');",
     "BEFORE DELETE ON audit_chain BEGIN\n                SELECT 1; -- MUTATION: delete accepted"),
    ("trace observer skipped", "src/planpilot/tools/middleware.py",
     "                self._observer(event)",
     "                pass  # MUTATION: trace observer skipped"),
    ("secret redaction disabled", "src/planpilot/audit.py",
     "    for pattern in _SECRET_PATTERNS:",
     "    for pattern in ():  # MUTATION: redaction disabled"),
    ("declared injection type ignored", "src/planpilot/workflow/events.py",
     ' or kind == "PROMPT_INJECTION"', " or False"),
    ("missing logger no longer fails closed", "src/planpilot/workflow/events.py",
     '                    raise RuntimeError("prompt injection requires the authoritative security logger")',
     "                    return {'state': self.state, 'candidates': [], 'security_events': [], 'traces': []}"),
    ("quarantine validation discarded", "src/planpilot/factory_state.py",
     "        validation = deepcopy(validation) if validation is not None else {",
     "        validation = {  # MUTATION: caller quarantine discarded"),
    ("security hash loses previous link", "src/planpilot/audit.py",
     "            (self.expected_head + canonical(self.record)).encode(\"utf-8\")",
     "            (GENESIS + canonical(self.record)).encode(\"utf-8\")"),
    ("trace identity no longer derived", "src/planpilot/audit.py",
     '                "trace_id": f"{correlation_id}:{sequence}",',
     '                "trace_id": correlation_id,  # MUTATION: sequence removed'),
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    target = tmp_path_factory.mktemp("security-audit-negctl") / "repo"
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", ".venv*", "__pycache__", "*.pyc", "tests/evidence"),
    )
    return target


def test_security_audit_mutations_are_all_caught_and_real_tree_is_unchanged(sandbox):
    before = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS}
    env = dict(os.environ, PYTHONPATH=str(sandbox / "src"))
    caught = escaped = broken = 0
    for label, relative, old, new in MUTATIONS:
        target = sandbox / relative
        # Byte-preserving restore (PlanStore/Approval negctl discipline):
        # text-mode writes on Windows normalise line endings and would break
        # the byte-identity assertion below.
        pristine_bytes = target.read_bytes()
        pristine = pristine_bytes.decode("utf-8")
        try:
            target.write_bytes(once(pristine, old, new, label).encode("utf-8"))
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *FOCUSED, "-q", "--no-header", "-p", "no:cacheprovider"],
                cwd=sandbox, env=env, capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                escaped += 1
                pytest.fail(f"mutation escaped: {label}")
            caught += 1
        except AssertionError:
            broken += 1
            raise
        finally:
            target.write_bytes(pristine_bytes)
    after = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS}
    assert before == after
    assert all((sandbox / path).read_bytes() == (ROOT / path).read_bytes() for path in SUBJECTS)
    print(f"SECURITY AUDIT NEGATIVE CONTROL | caught={caught} escaped={escaped} broken={broken} of {len(MUTATIONS)}")
