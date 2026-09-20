"""Bounded Bedrock Converse requests with server-only bearer authentication."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import re
import socket
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from planpilot.clock import SGT

DEFAULT_BEDROCK_REGION = 'ap-southeast-1'
DEFAULT_BEDROCK_MODEL = 'global.anthropic.claude-sonnet-4-5-20250929-v1:0'


class InferenceError(RuntimeError):
    """Safe, user-visible inference error without credentials or provider body."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider_error_detail(error, token):
    """Read bounded AWS message variants and an error type, never a raw body."""
    message = ''
    headers = error.headers or {}
    error_type = headers.get('x-amzn-errortype', '')
    try:
        raw = error.read(16385)
        if len(raw) <= 16384:
            value = json.loads(raw)
            if isinstance(value, dict):
                for name in ('message', 'Message'):
                    if isinstance(value.get(name), str) and value[name].strip():
                        message = value[name]
                        break
                if not error_type:
                    error_type = value.get('__type', '')
    except (OSError, ValueError, TypeError):
        pass
    finally:
        error.close()
    if isinstance(error_type, str):
        error_type = error_type.rsplit('#', 1)[-1].split(':', 1)[0]
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,79}', error_type):
            message = 'AWS error type: ' + error_type + ('; ' + message if message else '')
    if token:
        message = message.replace(token, '[redacted]')
    message = re.sub(r'(?i)bearer\s+\S+', 'Bearer [redacted]', message)
    message = re.sub(r'(?i)((?:api[_ -]?key|secret(?:[_ -]?access[_ -]?key)?|session[_ -]?token)\s*[:=]\s*)\S+',
                     r'\1[redacted]', message)
    message = re.sub(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', '[redacted]', message)
    message = re.sub(r'\b[A-Za-z0-9+/=_-]{80,}\b', '[redacted]', message)
    message = re.sub(r'\b\d{12}\b', '[account]', message)
    message = ' '.join(message.split())
    if len(message) > 1200:
        message = message[:1200] + ' [message truncated]'
    request_id = headers.get('x-amzn-requestid', '')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', request_id):
        request_id = ''
    return message, request_id


class BedrockClient:
    def __init__(self, db, *, region=None, model=None, token=None, transport=None,
                 calendar_clock=None):
        self.db = db
        # Billing-calendar hook for tests only (inject FixedClock); production
        # leaves it None and the real SGT wall clock below is the only source.
        self._calendar_clock = calendar_clock
        self.region = region or os.environ.get('PLANPILOT_BEDROCK_REGION', DEFAULT_BEDROCK_REGION)
        self.model = model or os.environ.get('PLANPILOT_BEDROCK_MODEL', DEFAULT_BEDROCK_MODEL)
        if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', self.region):
            raise ValueError('Invalid Bedrock region')
        if not self.model or len(self.model) > 2048 or any(c.isspace() for c in self.model):
            raise ValueError('Invalid Bedrock model identifier')
        if self.model != DEFAULT_BEDROCK_MODEL:
            raise ValueError(
                'Bedrock model must match the contract-pinned Claude Sonnet 4.5 inference profile'
            )
        self._token = token
        self.transport = transport or build_opener(NoRedirect()).open
        self.daily_limit = int(os.environ.get('PLANPILOT_BEDROCK_DAILY_TOKENS', '100000'))
        if self.daily_limit < 1:
            raise ValueError('Daily inference token limit must be positive')
        with db.lock:
            db.conn.execute('''CREATE TABLE IF NOT EXISTS inference_calls (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, day TEXT NOT NULL,
                actor TEXT NOT NULL, charged_tokens INTEGER NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, status TEXT NOT NULL)''')

    def _credential(self):
        token = self._token
        filename = os.environ.get('PLANPILOT_BEDROCK_KEY_FILE')
        if token is None and filename:
            try:
                with Path(filename).open('r', encoding='utf-8-sig') as stream:
                    token = stream.read(16385).strip()
            except (OSError, UnicodeError):
                raise InferenceError('无法读取 Bedrock 密钥文件。') from None
        elif token is None:
            token = os.environ.get('PLANPILOT_BEDROCK_API_KEY')
            if not token:
                raise InferenceError('未配置 Bedrock 密钥；请使用启动脚本的 -Bedrock 参数。')
        if not isinstance(token, str) or not token or len(token) > 16384 or not token.isascii() or any(c.isspace() for c in token):
            raise InferenceError('Bedrock 密钥文件必须只包含单行 API Key。')
        return token

    def status(self):
        try:
            self._credential()
            configured = True
        except InferenceError:
            configured = False
        return {'configured': configured, 'region': self.region, 'model': self.model,
                'mode': 'bedrock' if configured else 'local', 'connection_verified': False,
                'network_enabled': os.environ.get('PLANPILOT_FORBID_LLM_NETWORK', '1') == '0'}

    def converse(self, system, prompt, *, run_id, actor, max_tokens=2048):
        if os.environ.get('PLANPILOT_FORBID_LLM_NETWORK', '1') != '0':
            raise InferenceError('模型网络调用已禁用；仅在部署或演示时使用 -Bedrock 参数启用。')
        if not 1 <= max_tokens <= 4096:
            raise InferenceError('输出令牌上限必须介于 1 和 4096。')
        payload = {'system': [{'text': system}],
                   'messages': [{'role': 'user', 'content': [{'text': prompt}]}],
                   'inferenceConfig': {'temperature': 0, 'maxTokens': max_tokens}}
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        # UTF-8 byte count is a conservative upper bound for tokenizer input.
        if len(raw) > 24000:
            raise InferenceError('输入摘要超过本次模型调用上限，请缩小数据范围。')
        token = self._credential()
        # BILLING time, not business time: the daily cap is a cost guard
        # measured per real calendar day (contract cost controls). Sharing
        # the ScenarioClock would stack every demo onto the fictional
        # 2026-09-14 and reset the quota whenever scenario time crossed
        # midnight mid-session (G1.0.2 P1). Like token expiry, this stays
        # on the real wall clock — the audit/decision timestamp right
        # below still uses the server clock.
        call_id = str(uuid.uuid4())
        calendar = (self._calendar_clock.now() if self._calendar_clock
                    else datetime.now(SGT).isoformat())
        day = calendar[:10]
        reserved = len(raw) + max_tokens
        with self.db.transaction():
            calls = self.db.conn.execute('SELECT count(*) FROM inference_calls WHERE run_id=?', (run_id,)).fetchone()[0]
            used = self.db.conn.execute('SELECT coalesce(sum(charged_tokens),0) FROM inference_calls WHERE day=?', (day,)).fetchone()[0]
            if calls >= 2 or used + reserved > self.daily_limit:
                raise InferenceError('本次调用次数或每日令牌预算已达到上限。')
            self.db.conn.execute('INSERT INTO inference_calls VALUES(?,?,?,?,?,?,?,?)',
                                 (call_id, run_id, day, actor, reserved, None, None, 'started'))
        url = f'https://bedrock-runtime.{self.region}.amazonaws.com/model/{quote(self.model, safe="")}/converse'
        request = Request(url, data=raw, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, method='POST')
        try:
            with self.transport(request, timeout=20) as response:
                data = response.read(1048577)
            if len(data) > 1048576:
                raise InferenceError('Bedrock 响应超出大小上限。')
            result = json.loads(data)
            usage = result['usage']
            inputs, outputs = usage['inputTokens'], usage['outputTokens']
            if type(inputs) is not int or type(outputs) is not int or min(inputs, outputs) < 0:
                raise InferenceError('Bedrock 返回了无效的用量记录。')
            with self.db.transaction():
                self.db.conn.execute('UPDATE inference_calls SET charged_tokens=?,input_tokens=?,output_tokens=?,status=? WHERE id=?',
                                     (inputs + outputs, inputs, outputs, 'received', call_id))
                self.db._audit(None, actor, 'inference_usage', {'run_id': run_id, 'call_id': call_id,
                               'input_tokens': inputs, 'output_tokens': outputs, 'region': self.region, 'model': self.model})
            if result.get('stopReason') != 'end_turn':
                raise InferenceError('模型未完整结束回复，可能达到输出上限或被安全策略拦截。')
            blocks = result['output']['message']['content']
            text = '\n'.join(block['text'] for block in blocks if 'text' in block)
            if not text.strip():
                raise InferenceError('Bedrock 未返回文本回复。')
            return text
        except HTTPError as exc:
            code = exc.code
            detail, request_id = provider_error_detail(exc, token)
            messages = {
                400: 'Bedrock 拒绝请求参数。',
                401: 'Bedrock 密钥无效或已过期。',
                403: 'Bedrock 拒绝访问；请检查密钥有效期、模型权限及提供商首次使用要求。',
                404: 'Bedrock 未找到模型；请检查区域和模型或推理配置文件 ID。',
                429: 'Bedrock 调用限流，请稍后手动重试。'}
            message = messages.get(code, f'Bedrock 请求失败（HTTP {code}），请稍后重试。')
            if detail:
                message += '\nAWS 原因：' + detail
                if 'inference profile' in detail.lower() and 'on-demand' in detail.lower():
                    message += '\n该模型不能用当前基础模型 ID 按需调用。请从 Bedrock 控制台复制可从当前区域调用的推理配置文件 ID 或 ARN，通过 -BedrockModel 指定；程序不会自动切换区域或推理路由。'
            else:
                message += '\nAWS 未返回可安全展示的 JSON message。'
            message += f'\n区域：{self.region}；模型：{self.model}'
            if request_id:
                message += '\nAWS Request ID：' + request_id
            raise InferenceError(message) from None
        except (URLError, socket.timeout, TimeoutError, OSError):
            raise InferenceError('无法连接 Bedrock 或请求超时；未自动重试。') from None
        except (KeyError, TypeError, ValueError):
            raise InferenceError('Bedrock 响应格式无效。') from None
        finally:
            with self.db.transaction():
                self.db.conn.execute("UPDATE inference_calls SET status='unknown' WHERE id=? AND status='started'", (call_id,))
