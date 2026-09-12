"""Shared pytest configuration.

Adds src/ to sys.path so `import planpilot` works without installation, and
asserts at session start that (a) the on-disk contract is the one this code was
written against and (b) every fixture conforms to it. A drifted contract must
fail loudly here, not as a confusing assertion deep inside a test.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def pytest_configure(config):
    """Fail fast if the contract under test is not the one this code targets."""
    from planpilot import CONTRACT_SHA256
    import _fixtures as F

    actual = hashlib.sha256(F.CONTRACT_PATH.read_bytes()).hexdigest()
    if actual != CONTRACT_SHA256:
        raise RuntimeError(
            f"contract drift detected\n"
            f"  expected (planpilot.CONTRACT_SHA256): {CONTRACT_SHA256}\n"
            f"  on disk  ({F.CONTRACT_PATH.name}):      {actual}\n"
            f"The implementation was written against a different contract revision. "
            f"Re-run the spec or update CONTRACT_SHA256 deliberately."
        )
    # every fixture must still conform to the contract schemas
    F.self_test()


@pytest.fixture(scope="session")
def fixtures():
    import _fixtures as F
    return F
