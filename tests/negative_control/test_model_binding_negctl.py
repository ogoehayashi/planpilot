"""Disposable-copy mutations for the contract-pinned Bedrock model binding."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SUBJECTS = (
    "src/planpilot/inference/bedrock_client.py", "src/planpilot/agent/bedrock.py",
    "tools/start_local.ps1", "tools/build_team_package.py", ".env.example",
)
FOCUSED = ("tests/unit/test_bedrock_web.py",)
TEST_FILTER = (
    "default_binding_is_contract_pinned or non_contract_model_override or "
    "submission_surfaces_do_not_default_to_nova or provider_specific_io_has_one_authoritative_module"
)


def once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"[{label}] broken anchor: {count} occurrences")
    return text.replace(old, new)


PINNED = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
NOVA = "amazon.nova-pro-v1:0"
MUTATIONS = (
    ("runtime defaults to Nova", "src/planpilot/inference/bedrock_client.py",
     f"DEFAULT_BEDROCK_MODEL = '{PINNED}'", f"DEFAULT_BEDROCK_MODEL = '{NOVA}'"),
    ("non-contract override accepted", "src/planpilot/inference/bedrock_client.py",
     "        if self.model != DEFAULT_BEDROCK_MODEL:", "        if False:  # MUTATION"),
    ("launcher defaults to Nova", "tools/start_local.ps1",
     f"[string]$BedrockModel = '{PINNED}'", f"[string]$BedrockModel = '{NOVA}'"),
    ("package advertises Nova", "tools/build_team_package.py",
     f"'default_model': '{PINNED}'", f"'default_model': '{NOVA}'"),
    ("environment binding removed", ".env.example",
     f"PLANPILOT_BEDROCK_MODEL={PINNED}", "PLANPILOT_BEDROCK_MODEL="),
    ("parallel provider SDK restored", "src/planpilot/agent/bedrock.py",
     "from __future__ import annotations\n", "from __future__ import annotations\nimport boto3  # MUTATION\n"),
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    target = tmp_path_factory.mktemp("model-binding-negctl") / "repo"
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns(
            ".git", ".pytest_cache", ".venv*", "__pycache__", "*.pyc", "tests/evidence"
        ),
    )
    return target


def test_model_binding_mutations_are_all_caught_and_source_is_unchanged(sandbox):
    before = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS}
    env = dict(os.environ, PYTHONPATH=str(sandbox / "src"))
    caught = escaped = broken = 0
    for label, relative, old, new in MUTATIONS:
        target = sandbox / relative
        # Byte-preserving restore (same discipline as the PlanStore/Approval
        # negctls): text-mode writes on Windows would normalise line endings
        # and break the byte-identity assertion below.
        pristine_bytes = target.read_bytes()
        pristine = pristine_bytes.decode("utf-8")
        try:
            target.write_bytes(once(pristine, old, new, label).encode("utf-8"))
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *FOCUSED, "-q", "-k", TEST_FILTER,
                 "--no-header", "-p", "no:cacheprovider"],
                cwd=sandbox, env=env, capture_output=True, text=True, timeout=90,
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
    print(
        f"MODEL BINDING NEGATIVE CONTROL | caught={caught} escaped={escaped} "
        f"broken={broken} of {len(MUTATIONS)}"
    )
