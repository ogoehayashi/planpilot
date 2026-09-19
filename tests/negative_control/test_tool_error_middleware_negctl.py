"""Disposable-copy mutation control for the public tool middleware."""
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
    "src/planpilot/tools/middleware.py",
    "src/planpilot/tools/errors.py",
    "src/planpilot/tools/serialization.py",
)
FOCUSED = ("tests/unit/test_tool_error_middleware.py", "tests/unit/test_engine_utilities.py")


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"[{label}] broken anchor: expected 1 occurrence, found {count}")
    return text.replace(old, new)


MUTATIONS = (
    ("skip input validation", "src/planpilot/tools/middleware.py",
     '            validate_tool_payload(dict(payload), tool_name, "input_schema", f"{tool_name}_input")',
     '            pass  # MUTATION: input validation skipped'),
    ("skip output validation", "src/planpilot/tools/middleware.py",
     '                            validate_tool_payload(candidate_copy, tool_name, "output_schema", f"{tool_name}_output")',
     '                            pass  # MUTATION: output validation skipped'),
    ("commit before prepare", "src/planpilot/tools/middleware.py",
     '                    candidate = prepared.prepare(deepcopy(dict(payload)), tool_context)',
     '                    prepared.commit()  # MUTATION: early commit\n                    candidate = prepared.prepare(deepcopy(dict(payload)), tool_context)'),
    ("accept spoofed exception", "src/planpilot/tools/errors.py",
     '    return internal_error(type(exc).__name__)',
     '    return FrameworkDomainError(exc.code, "spoofed", exc.details) if hasattr(exc, "code") else internal_error(type(exc).__name__)'),
    ("override retryability", "src/planpilot/tools/errors.py",
     '        return resolve_error(self.code).retryable',
     '        return False  # MUTATION: caller-independent registry ignored'),
    ("count characters not bytes", "src/planpilot/tools/serialization.py",
     '    if len(wire) > MAX_FAILURE_BYTES:',
     '    if len(wire.decode("utf-8")) > MAX_FAILURE_BYTES:'),
    ("raw truncate oversized JSON", "src/planpilot/tools/serialization.py",
     '        raise WireSerializationError("failure envelope exceeds 4096 UTF-8 bytes")',
     '        return wire[:MAX_FAILURE_BYTES]  # MUTATION: invalid raw truncation'),
    ("leak exception text", "src/planpilot/tools/errors.py",
     '    return internal_error(type(exc).__name__)',
     '    return FrameworkDomainError("INTERNAL_ERROR", "unexpected", {"diagnostic_class": type(exc).__name__, "safe_detail": str(exc)[:500]})'),
    ("catch process-control exception", "src/planpilot/tools/middleware.py",
     '                except Exception as exc:\n                    outcome = self._failure(tool_name, correlation_id, exc)',
     '                except BaseException as exc:\n                    outcome = self._failure(tool_name, correlation_id, exc)'),
    ("hidden retry", "src/planpilot/tools/middleware.py",
     '                except Exception as exc:\n                    outcome = self._failure(tool_name, correlation_id, exc)',
     '                except Exception as exc:\n                    prepared.prepare(deepcopy(dict(payload)), tool_context)  # MUTATION: hidden retry\n                    outcome = self._failure(tool_name, correlation_id, exc)'),
    ("observer rewrites success", "src/planpilot/tools/middleware.py",
     '            except Exception:\n                pass\n        return outcome',
     '            except Exception:\n                return self._failure(outcome.tool_name, outcome.correlation_id, internal_error("ObserverFailure"))\n        return outcome'),
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    target = tmp_path_factory.mktemp("tool-middleware-negctl") / "repo"
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc", "tests/evidence"),
    )
    return target


def test_all_mutations_are_caught_in_disposable_copy_and_source_is_unchanged(sandbox):
    before = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS}
    caught = escaped = broken = 0
    env = dict(os.environ, PYTHONPATH=str(sandbox / "src"))

    for label, relative, old, new in MUTATIONS:
        target = sandbox / relative
        pristine = target.read_text(encoding="utf-8")
        try:
            target.write_text(_replace_once(pristine, old, new, label), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *FOCUSED, "-q", "--no-header", "-p", "no:cacheprovider"],
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
            target.write_text(pristine, encoding="utf-8")

    after = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SUBJECTS}
    assert before == after
    assert all((sandbox / path).read_bytes() == (ROOT / path).read_bytes() for path in SUBJECTS)
    print(
        f"TOOL MIDDLEWARE NEGATIVE CONTROL | caught={caught} escaped={escaped} "
        f"broken={broken} of {len(MUTATIONS)}"
    )
