"""Formal evaluation must never be promoted by smoke or fabricated evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_contract_condition_is_preserved_and_blocked(tmp_path):
    runner = load_tool('run_evals')
    result = runner.run(tmp_path)
    contract = json.loads(runner.CONTRACT.read_text())
    assert (result['case_count'], result['passed'], result['failed'], result['blocked']) == (30, 0, 0, 30)
    assert result['runtime_evaluation'] == contract['release_readiness']['runtime_evaluation']
    assert [{k: row[k] for k in case} for row, case in zip(result['results'], contract['acceptance_tests'])] == contract['acceptance_tests']
    for row in result['results']:
        assert row['status'] == 'BLOCKED' and row['executed'] is False
        for needed in ('inputs', 'outputs', 'trace', 'digest', 'timing', 'approval', 'audit', 'executor'):
            assert needed in ' '.join(row['missing_evidence'])
    assert json.loads((tmp_path / 'EVIDENCE.json').read_text()) == result


@pytest.mark.parametrize('fabricated_check', [True, 100 < 300, {'deterministic': True, 'violations': []}])
def test_prior_green_or_placeholder_assertions_cannot_unlock_formal_cases(tmp_path, fabricated_check):
    (tmp_path / 'EVIDENCE.json').write_text(json.dumps({
        'passed': 30, 'blocked': 0, 'check': fabricated_check,
        'results': [{'case_id': f'EVAL-{i:03d}', 'status': 'PASS'} for i in range(1, 31)]}))
    completed = subprocess.run([sys.executable, str(ROOT / 'tools/run_evals.py'), '--output', str(tmp_path)],
                               capture_output=True, text=True)
    assert completed.returncode == 1, completed.stderr
    assert 'cases=30 passed=0 failed=0 blocked=30' in completed.stdout
    evidence = json.loads((tmp_path / 'EVIDENCE.json').read_text())
    assert {row['status'] for row in evidence['results']} == {'BLOCKED'}


@pytest.mark.parametrize('change', ['empty', 'duplicate', 'missing'])
def test_broken_case_inventory_fails_instead_of_emitting_green(tmp_path, monkeypatch, change):
    runner = load_tool('run_evals')
    contract = json.loads(runner.CONTRACT.read_text())
    cases = contract['acceptance_tests']
    contract['acceptance_tests'] = [] if change == 'empty' else cases[:-1] + [cases[0]] if change == 'duplicate' else cases[:-1]
    path = tmp_path / 'contract.json'
    path.write_text(json.dumps(contract))
    monkeypatch.setattr(runner, 'CONTRACT', path)
    with pytest.raises(ValueError):
        runner.run(tmp_path / 'out')
    assert not (tmp_path / 'out/EVIDENCE.json').exists()


def test_smoke_failure_is_nonzero_and_never_writes_formal_results(tmp_path, monkeypatch):
    smoke = load_tool('run_smoke_harness')
    def fail():
        raise AssertionError('injected failure')
    monkeypatch.setattr(smoke, 'CHECKS', (('injected_smoke_failure', fail),))
    assert smoke.main(['--output', str(tmp_path)]) == 1
    text = (tmp_path / 'SMOKE_EVIDENCE.json').read_text()
    result = json.loads(text)
    assert result['formal_acceptance'] is False
    assert result['failed'] == 1 and result['passed'] == 0
    assert 'EVAL-' not in text and 'case_id' not in text
    assert not (tmp_path / 'EVIDENCE.json').exists()


def test_smoke_cannot_write_into_formal_directory():
    smoke = load_tool('run_smoke_harness')
    with pytest.raises(ValueError):
        smoke.run(ROOT / 'tests/evidence/runtime-eval')


def test_optimized_python_cannot_skip_smoke_assertions(tmp_path):
    completed = subprocess.run([sys.executable, '-O', str(ROOT / 'tools/run_smoke_harness.py'), '--output', str(tmp_path)],
                               capture_output=True, text=True)
    assert completed.returncode != 0
    assert 'without -O' in completed.stderr
    assert not (tmp_path / 'SMOKE_EVIDENCE.json').exists()


def test_checked_in_formal_evidence_and_overview_do_not_claim_success():
    result = json.loads((ROOT / 'tests/evidence/runtime-eval/EVIDENCE.json').read_text())
    assert (result['passed'], result['failed'], result['blocked']) == (0, 0, 30)
    assert {r['status'] for r in result['results']} == {'BLOCKED'}
    overview = (ROOT / 'PROJECT_OVERVIEW_BILINGUAL.md').read_text()
    assert '30 PASS / 0 FAIL / 0 BLOCKED' not in overview
    assert overview.count('0 PASS / 0 FAIL / 30 BLOCKED') == 2
