"""Offline provider and web boundary tests. No Bedrock connections or credentials."""
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import threading
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from planpilot.agent.chat import ChatService
from planpilot.inference.bedrock_client import (
    DEFAULT_BEDROCK_MODEL,
    DEFAULT_BEDROCK_REGION,
    BedrockClient,
    InferenceError,
    NoRedirect,
)
from planpilot.persistence import Database
from planpilot.security import issue_token
from tools.build_team_package import build


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def mocked_inference_enabled(monkeypatch):
    # Every provider in this module supplies a fake transport; no AWS calls.
    monkeypatch.setenv('PLANPILOT_FORBID_LLM_NETWORK', '0')


@pytest.fixture
def db():
    value = Database(':memory:')
    yield value
    value.close()


def response(text, stop='end_turn'):
    return BytesIO(json.dumps({'usage': {'inputTokens': 30, 'outputTokens': 20},
                              'stopReason': stop, 'output': {'message': {'content': [{'text': text}]}}}).encode())


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return response(reply)


def client(db, transport):
    return BedrockClient(db, token='test-only-credential', transport=transport)


def intent(action):
    return json.dumps({'action': action, 'response': '请通过审批界面操作。' if action == 'reply' else ''})


def test_regional_bearer_transport_usage_and_audit(db):
    transport = Transport('hello')
    provider = client(db, transport)
    assert provider.converse('system', 'question', run_id='r', actor='planner') == 'hello'
    request = transport.requests[0]
    assert request.full_url == ('https://bedrock-runtime.ap-southeast-1.amazonaws.com/model/'
                                'global.anthropic.claude-sonnet-4-5-20250929-v1%3A0/converse')
    assert request.get_header('Authorization') == 'Bearer test-only-credential'
    row = db.conn.execute('SELECT * FROM inference_calls').fetchone()
    assert row['charged_tokens'] == 50 and row['status'] == 'received'
    assert db.verify_audit()
    assert 'test-only-credential' not in str(db.conn.execute('SELECT record FROM audit_chain').fetchall())


def test_default_binding_is_contract_pinned_claude_sonnet_45(db, monkeypatch):
    monkeypatch.delenv('PLANPILOT_BEDROCK_REGION', raising=False)
    monkeypatch.delenv('PLANPILOT_BEDROCK_MODEL', raising=False)
    provider = BedrockClient(db, token='test-only-credential', transport=Transport())
    assert provider.region == DEFAULT_BEDROCK_REGION == 'ap-southeast-1'
    assert provider.model == DEFAULT_BEDROCK_MODEL == (
        'global.anthropic.claude-sonnet-4-5-20250929-v1:0'
    )
    assert provider.status()['connection_verified'] is False
    assert provider.transport.requests == []


def test_non_contract_model_override_fails_closed(db):
    with pytest.raises(ValueError, match='contract-pinned Claude Sonnet 4.5'):
        BedrockClient(db, model='amazon.nova-pro-v1:0', token='test-only-credential',
                      transport=Transport())


def test_submission_surfaces_do_not_default_to_nova():
    checked = (
        'src/planpilot/inference/bedrock_client.py', 'tools/start_local.ps1',
        'tools/build_team_package.py', '.env.example', 'Dockerfile', 'compose.yaml',
        'START_HERE.md', 'PRODUCT_RUNBOOK.md', 'docs/bedrock-web-guide.md',
    )
    for relative in checked:
        text = (ROOT / relative).read_text(encoding='utf-8')
        assert 'amazon.nova-pro-v1:0' not in text, relative
        assert 'global.anthropic.claude-sonnet-4-5-20250929-v1:0' in text, relative


def test_provider_specific_io_has_one_authoritative_module():
    compatibility = (ROOT / 'src/planpilot/agent/bedrock.py').read_text(encoding='utf-8')
    assert 'boto3' not in compatibility
    assert "client.converse" not in compatibility
    assert "bedrock-runtime" not in compatibility


def test_generated_handoff_has_one_verified_contract_bound_manifest(tmp_path):
    destination = tmp_path / 'PlanPilot-handoff.zip'
    build(destination, [])
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist().count('PlanPilot/PACKAGE_MANIFEST.json') == 1
        assert archive.testzip() is None
        manifest = json.loads(archive.read('PlanPilot/PACKAGE_MANIFEST.json'))
    assert manifest['default_region'] == DEFAULT_BEDROCK_REGION
    assert manifest['default_model'] == DEFAULT_BEDROCK_MODEL


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429, 500, 302])
def test_provider_errors_do_not_echo_body_or_credentials(db, status):
    error = HTTPError('https://example.invalid', status, 'secret-provider-message', {}, BytesIO(b'private-body'))
    provider = client(db, Transport(error))
    with pytest.raises(InferenceError) as caught:
        provider.converse('system', 'question', run_id='r', actor='planner')
    assert 'secret-provider-message' not in str(caught.value)
    assert 'private-body' not in str(caught.value)
    row = db.conn.execute('SELECT * FROM inference_calls').fetchone()
    assert row['status'] == 'unknown' and row['charged_tokens'] > 0


def test_redirects_are_not_followed():
    assert NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid') is None


def test_inference_profile_error_preserves_actionable_aws_reason(db):
    detail = 'Invocation of model with on-demand throughput is not supported. Retry with an inference profile.'
    error = HTTPError('https://example.invalid', 400, '', {'x-amzn-requestid': 'test-request-123'},
                      BytesIO(json.dumps({'message': detail}).encode()))
    with pytest.raises(InferenceError) as caught:
        client(db, Transport(error)).converse('system', 'question', run_id='r', actor='planner')
    text = str(caught.value)
    assert detail in text
    assert '-BedrockModel' in text and 'ap-southeast-1' in text
    assert 'test-request-123' in text


def test_provider_message_redacts_credentials_before_display(db):
    detail = 'Invalid key test-only-credential; Bearer other-token api_key=another-token account 123456789012'
    error = HTTPError('https://example.invalid', 400, '', {},
                      BytesIO(json.dumps({'message': detail, 'private': 'never-show-this'}).encode()))
    with pytest.raises(InferenceError) as caught:
        client(db, Transport(error)).converse('system', 'question', run_id='r', actor='planner')
    text = str(caught.value)
    for secret in ('test-only-credential', 'other-token', 'another-token', '123456789012', 'never-show-this'):
        assert secret not in text
    assert '[redacted]' in text


@pytest.mark.parametrize('field', ['message', 'Message'])
def test_aws_message_case_variants_are_visible_and_redacted(db, field):
    error = HTTPError('https://example.invalid', 403, '', {'x-amzn-errortype': 'AccessDeniedException'},
                      BytesIO(json.dumps({field: 'Denied test-only-credential'}).encode()))
    with pytest.raises(InferenceError) as caught:
        client(db, Transport(error)).converse('system', 'question', run_id='r', actor='planner')
    assert 'AccessDeniedException' in str(caught.value)
    assert 'Denied [redacted]' in str(caught.value)
    assert 'test-only-credential' not in str(caught.value)


def test_aws_error_type_can_diagnose_empty_message(db):
    error = HTTPError('https://example.invalid', 403, '', {},
                      BytesIO(json.dumps({'__type': 'aws.service#UnrecognizedClientException'}).encode()))
    with pytest.raises(InferenceError) as caught:
        client(db, Transport(error)).converse('system', 'question', run_id='r', actor='planner')
    assert 'UnrecognizedClientException' in str(caught.value)


def test_provider_detail_is_bounded_and_does_not_guess_profile_error(db):
    error = HTTPError('https://example.invalid', 400, '', {},
                      BytesIO(json.dumps({'message': 'Unsupported parameter. ' * 400}).encode()))
    with pytest.raises(InferenceError) as caught:
        client(db, Transport(error)).converse('system', 'question', run_id='r', actor='planner')
    text = str(caught.value)
    assert len(text) < 1600
    assert '-BedrockModel' not in text


def test_oversized_error_body_is_not_forwarded(db):
    error = HTTPError('https://example.invalid', 400, '', {},
                      BytesIO(json.dumps({'message': 'private-body' * 2000}).encode()))
    with pytest.raises(InferenceError) as caught:
        client(db, Transport(error)).converse('system', 'question', run_id='r', actor='planner')
    assert 'private-body' not in str(caught.value)


def test_network_guard_refuses_before_transport(db, monkeypatch):
    monkeypatch.setenv('PLANPILOT_FORBID_LLM_NETWORK', '1')
    transport = Transport()
    with pytest.raises(InferenceError):
        client(db, transport).converse('system', 'question', run_id='r', actor='planner')
    assert transport.requests == []


def test_incomplete_output_is_rejected_but_actual_usage_is_saved(db):
    provider = client(db, lambda request, timeout: response('partial', 'max_tokens'))
    with pytest.raises(InferenceError):
        provider.converse('system', 'question', run_id='r', actor='planner')
    assert db.conn.execute('SELECT charged_tokens FROM inference_calls').fetchone()[0] == 50


def test_unexpected_response_is_not_shown_to_user(db):
    provider = client(db, lambda request, timeout: BytesIO(b'not-json-private-body'))
    with pytest.raises(InferenceError) as caught:
        provider.converse('system', 'question', run_id='r', actor='planner')
    assert 'private-body' not in str(caught.value)


def test_limits_refuse_network_and_preserve_accounting(db):
    transport = Transport('one', 'two')
    provider = client(db, transport)
    for _ in range(2):
        provider.converse('system', 'question', run_id='same', actor='planner')
    with pytest.raises(InferenceError):
        provider.converse('system', 'question', run_id='same', actor='planner')
    provider.daily_limit = 100
    with pytest.raises(InferenceError):
        provider.converse('system', 'question', run_id='new', actor='planner')
    with pytest.raises(InferenceError):
        provider.converse('system', 'x' * 24001, run_id='new', actor='planner')
    assert len(transport.requests) == 2


def test_key_file_and_status_never_call_network(db, tmp_path, monkeypatch):
    keyfile = tmp_path / 'credential.txt'
    keyfile.write_text('test-key\n', encoding='utf-8-sig')
    monkeypatch.delenv('PLANPILOT_BEDROCK_API_KEY', raising=False)
    monkeypatch.setenv('PLANPILOT_BEDROCK_KEY_FILE', str(keyfile))
    transport = Transport()
    provider = BedrockClient(db, transport=transport)
    status = provider.status()
    assert status['configured'] and not status['connection_verified']
    assert 'test-key' not in json.dumps(status)
    assert transport.requests == []
    keyfile.write_text('key\nextra', encoding='utf-8')
    assert not provider.status()['configured']


def test_generation_uses_real_solver_and_excludes_raw_data_from_model(db):
    transport = Transport(intent('generate'), '已生成并独立验证三个方案。')
    chat = ChatService(db, client(db, transport), ROOT / 'data')
    raw = json.loads((ROOT / 'data/factory_demo_v18.json').read_text(encoding='utf-8-sig'))
    raw['private_note'] = 'do-not-send-this-factory-text'
    result = chat.run({'message': '生成三个方案', 'factory_data': raw}, 'planner')
    assert result['plan']['stored_plan_count'] == 3
    prompts = '\n'.join(req.data.decode() for req in transport.requests)
    assert 'do-not-send-this-factory-text' not in prompts
    assert '"operations"' not in prompts and '"factory_data"' not in prompts
    assert 'validated_summary' in prompts
    assert db.conn.execute('SELECT revision FROM authority_state').fetchone()[0] == 3
    assert db.verify_audit()


def test_explicit_key_file_overrides_stale_environment_and_reloads(db, tmp_path, monkeypatch):
    keyfile = tmp_path / 'credential.txt'
    keyfile.write_text('first-file-key', encoding='utf-8')
    monkeypatch.setenv('PLANPILOT_BEDROCK_KEY_FILE', str(keyfile))
    monkeypatch.setenv('PLANPILOT_BEDROCK_API_KEY', 'stale-env-key')
    transport = Transport('one', 'two')
    provider = BedrockClient(db, transport=transport)
    provider.converse('system', 'question', run_id='r', actor='planner')
    keyfile.write_text('updated-file-key', encoding='utf-8')
    provider.converse('system', 'question', run_id='r', actor='planner')
    assert transport.requests[0].get_header('Authorization') == 'Bearer first-file-key'
    assert transport.requests[1].get_header('Authorization') == 'Bearer updated-file-key'
    keyfile.unlink()
    with pytest.raises(InferenceError):
        provider._credential()


def test_generation_survives_explanation_failure_after_authoritative_write(db):
    transport = Transport(intent('generate'), TimeoutError())
    result = ChatService(db, client(db, transport), ROOT / 'data').run({'message': '生成'}, 'planner')
    assert result['warning']
    assert len(transport.requests) == 2
    assert db.conn.execute('SELECT revision FROM authority_state').fetchone()[0] == 3


def test_invalid_intent_never_schedules(db):
    transport = Transport('{"action":"publish_plan","response":"approved"}')
    with pytest.raises(InferenceError):
        ChatService(db, client(db, transport), ROOT / 'examples').run({'message': '发布'}, 'planner')
    assert db.conn.execute('SELECT revision FROM authority_state').fetchone()[0] == 0


def test_reply_and_explain_without_plan_do_not_generate(db):
    for action in ('reply', 'explain'):
        result = ChatService(db, client(db, Transport(intent(action))), ROOT / 'examples').run({'message': '帮助'}, 'planner')
        assert 'plan' not in result
    assert db.conn.execute('SELECT revision FROM authority_state').fetchone()[0] == 0


def test_factory_path_cannot_escape_root(db):
    service = ChatService(db, client(db, Transport(intent('generate'))), ROOT / 'examples')
    with pytest.raises(PermissionError):
        service.run({'message': '生成', 'factory_file': '../requirements.txt'}, 'planner')


def test_http_auth_and_end_to_end_mocked_provider(db):
    spec = importlib.util.spec_from_file_location('bedrock_web_test_api', ROOT / 'tools/api_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret = 'test-secret-' * 4
    server = module.Server(('127.0.0.1', 0), db, secret, ROOT / 'data')
    provider = client(db, Transport(intent('generate'), '三个方案已通过独立验证。'))
    server.inference = provider
    server.chat.client = provider
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    token = issue_token('planner-test', 'planner', secret)

    def post(body, authorized=True):
        headers = {'Content-Type': 'application/json'}
        if authorized:
            headers['Authorization'] = 'Bearer ' + token
        return urlopen(Request(base + '/agent/chat', data=json.dumps(body).encode(), headers=headers), timeout=10)

    try:
        with pytest.raises(HTTPError) as denied:
            post({'message': '生成'}, False)
        assert denied.value.code == 403
        assert not provider.transport.requests
        with post({'message': '生成三个方案'}) as response:
            generated = json.load(response)
        assert generated['plan']['stored_plan_count'] == 3
        assert db.conn.execute('SELECT revision FROM authority_state').fetchone()[0] == 3
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_second_server_cannot_bind_same_address(db):
    spec = importlib.util.spec_from_file_location('bedrock_exclusive_port_test', ROOT / 'tools/api_server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.Server(('127.0.0.1', 0), db, 'test-secret-' * 4, ROOT / 'examples')
    try:
        with pytest.raises(OSError):
            module.Server(server.server_address, db, 'test-secret-' * 4, ROOT / 'examples')
    finally:
        server.server_close()


def test_daily_budget_uses_billing_calendar_not_scenario_clock(db, monkeypatch):
    """G1.0.2 review P1-4: the daily cost guard counts per REAL calendar day.
    A ScenarioClock-stamped DB must not stack every demo onto the fictional
    2026-09-14, and scenario midnight must not reset a quota that AWS bills
    are still spending. Production leaves calendar_clock unset (real SGT);
    the injected FixedClock here stands in for that wall clock."""
    from planpilot.clock import FixedClock, ScenarioClock
    scenario_db = Database(':memory:', clock=ScenarioClock("2026-09-14T08:00:00+08:00"))
    try:
        transport = Transport('hello')
        client = BedrockClient(
            scenario_db, token='test-only-credential', transport=transport,
            calendar_clock=FixedClock('2026-09-20T10:00:00+08:00'))
        client.converse('system', 'question', run_id='r1', actor='planner')
        days = [r['day'] for r in
                scenario_db.conn.execute('SELECT day FROM inference_calls').fetchall()]
        assert days == ['2026-09-20']       # billing calendar, not scenario date
        assert '2026-09-14' not in days
    finally:
        scenario_db.close()
