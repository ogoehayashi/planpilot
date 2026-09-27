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

# The hackathon gateway's model sometimes wraps its whole reply in a markdown
# code fence even when the prompt asks for raw JSON. Unwrap a SINGLE
# fully-fenced response so callers see clean output; partial fences are left
# alone.
_FENCE_RE = re.compile(r'^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?\s*```\s*$', re.DOTALL)


def _strip_code_fence(text):
    if not text:
        return text
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text


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
        # Optional LLM-gateway mode (the hackathon organizers' Bedrock-backed
        # gateway). When set, requests go to an OpenAI-compatible
        # /v1/chat/completions endpoint instead of the bedrock-runtime
        # Converse endpoint. Read at construction, like region/model.
        self.gateway_url = (os.environ.get('PLANPILOT_GATEWAY_URL') or '').strip().rstrip('/')
        # HTTP timeout for a single model call. The gateway path can be slower
        # than a direct regional endpoint, so allow an operator override.
        self.request_timeout = float(os.environ.get('PLANPILOT_LLM_TIMEOUT_S', '20'))
        if self.request_timeout <= 0:
            raise ValueError('LLM request timeout must be positive')
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
                raise InferenceError('Could not read the Bedrock key file.') from None
        elif token is None:
            token = os.environ.get('PLANPILOT_BEDROCK_API_KEY')
            if not token:
                raise InferenceError('Bedrock key not configured; use the -Bedrock option of the startup script.')
        if not isinstance(token, str) or not token or len(token) > 16384 or not token.isascii() or any(c.isspace() for c in token):
            raise InferenceError('The Bedrock key file must contain a single-line API key only.')
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
            raise InferenceError('Model network calls are disabled; enable them with the -Bedrock option only for deployment or demo.')
        if not 1 <= max_tokens <= 4096:
            raise InferenceError('The output token limit must be between 1 and 4096.')
        if self.gateway_url:
            payload = {'model': self.model,
                       'messages': [{'role': 'system', 'content': system},
                                    {'role': 'user', 'content': prompt}],
                       'temperature': 0,
                       'max_tokens': max_tokens}
        else:
            payload = {'system': [{'text': system}],
                       'messages': [{'role': 'user', 'content': [{'text': prompt}]}],
                       'inferenceConfig': {'temperature': 0, 'maxTokens': max_tokens}}
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        # UTF-8 byte count is a conservative upper bound for tokenizer input.
        if len(raw) > 24000:
            raise InferenceError('The input summary exceeds the limit for this model call; reduce the data scope.')
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
                raise InferenceError('The per-run call count or daily token budget has been reached.')
            self.db.conn.execute('INSERT INTO inference_calls VALUES(?,?,?,?,?,?,?,?)',
                                 (call_id, run_id, day, actor, reserved, None, None, 'started'))
        if self.gateway_url:
            url = self.gateway_url + '/v1/chat/completions'
        else:
            url = f'https://bedrock-runtime.{self.region}.amazonaws.com/model/{quote(self.model, safe="")}/converse'
        request = Request(url, data=raw, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, method='POST')
        try:
            with self.transport(request, timeout=self.request_timeout) as response:
                data = response.read(1048577)
            if len(data) > 1048576:
                raise InferenceError('The Bedrock response exceeded the size limit.')
            result = json.loads(data)
            usage = result['usage']
            if self.gateway_url:
                inputs, outputs = usage['prompt_tokens'], usage['completion_tokens']
            else:
                inputs, outputs = usage['inputTokens'], usage['outputTokens']
            if type(inputs) is not int or type(outputs) is not int or min(inputs, outputs) < 0:
                raise InferenceError('Bedrock returned an invalid usage record.')
            with self.db.transaction():
                self.db.conn.execute('UPDATE inference_calls SET charged_tokens=?,input_tokens=?,output_tokens=?,status=? WHERE id=?',
                                     (inputs + outputs, inputs, outputs, 'received', call_id))
                self.db._audit(None, actor, 'inference_usage', {'run_id': run_id, 'call_id': call_id,
                               'input_tokens': inputs, 'output_tokens': outputs, 'region': self.region, 'model': self.model})
            if self.gateway_url:
                choice = (result.get('choices') or [{}])[0]
                if choice.get('finish_reason') not in ('stop', 'end_turn'):
                    raise InferenceError('The model did not finish its reply; it may have hit the output limit or been blocked by a safety policy.')
                message = choice.get('message') or {}
                text = message.get('content') if isinstance(message.get('content'), str) else ''
                text = _strip_code_fence(text)
            else:
                if result.get('stopReason') != 'end_turn':
                    raise InferenceError('The model did not finish its reply; it may have hit the output limit or been blocked by a safety policy.')
                blocks = result['output']['message']['content']
                text = '\n'.join(block['text'] for block in blocks if 'text' in block)
            if not text.strip():
                raise InferenceError('Bedrock returned no text reply.')
            return text
        except HTTPError as exc:
            code = exc.code
            detail, request_id = provider_error_detail(exc, token)
            messages = {
                400: 'Bedrock rejected the request parameters.',
                401: 'The Bedrock key is invalid or expired.',
                403: 'Bedrock denied access; check the key expiry, model permissions, and the provider first-use requirements.',
                404: 'Bedrock could not find the model; check the region and the model or inference-profile ID.',
                429: 'Bedrock is rate-limiting; retry manually later.'}
            message = messages.get(code, f'Bedrock request failed (HTTP {code}); retry later.')
            if detail:
                message += '\nAWS reason: ' + detail
                if 'inference profile' in detail.lower() and 'on-demand' in detail.lower():
                    message += '\nThis model cannot be invoked on-demand with the current base model ID. Copy an inference-profile ID or ARN callable from the current region in the Bedrock console and pass it via -BedrockModel; the program will not switch region or inference routing automatically.'
            else:
                message += '\nAWS returned no JSON message that is safe to display.'
            message += f'\nRegion: {self.region}; model: {self.model}'
            if request_id:
                message += '\nAWS Request ID: ' + request_id
            raise InferenceError(message) from None
        except (URLError, socket.timeout, TimeoutError, OSError):
            raise InferenceError('Could not reach Bedrock or the request timed out; no automatic retry.') from None
        except (KeyError, TypeError, ValueError):
            raise InferenceError('The Bedrock response format is invalid.') from None
        finally:
            with self.db.transaction():
                self.db.conn.execute("UPDATE inference_calls SET status='unknown' WHERE id=? AND status='started'", (call_id,))
