#!/usr/bin/env python3
"""GenieX v0.11.0 gateway with optional v0.9.3 compatibility; stdlib only, never executes HA tools.

Only model catalog and chat completions are exposed. SSE bytes are forwarded
unchanged, with optional bounded observation for payload-free metrics.
The LAN listener is unauthenticated unless QWEN_GATEWAY_API_KEY is set. Do not expose it to the public Internet.
"""
import argparse
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import stat
import socket
import sys
import logging
from logging.handlers import RotatingFileHandler
import time
import uuid
import threading
from urllib.parse import urlsplit
from datetime import datetime

from qwen_metrics import RequestMetrics, ResponseObserver

MODEL = 'bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0'
MAX_BODY = 2 * 1024 * 1024
# Legacy v0.9.3 can return an empty response for an identical prompt. The
# explicit reset policy inserts a tool-free turn to discard the previous long
# prefix. The default reuse policy sends only the real request on v0.11.0.
RESET_MESSAGES = [{'role': 'system', 'content': 'Internal cache reset. Reply OK.'},
                  {'role': 'user', 'content': '.'}]


def normalize_request(body, model=MODEL, nctx=4096):
    if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
        raise ValueError('messages must be an array')
    body = dict(body)
    # No caller may redirect compute, enable thinking or enlarge the context.
    body.update(model=model, compute='HTP0', ngl=-1, nctx=nctx,
                enable_think=False, thinking=False, temperature=0.1, top_k=1, seed=42,
                repetition_penalty=1.0, presence_penalty=0.0,
                frequency_penalty=0.0)
    # Avoid undocumented/speculative/grammar paths on the pinned runtime.
    for key in ('spec_type', 'spec_draft_model', 'spec_n_max', 'spec_n_min',
                'spec_p_min', 'grammar_path', 'grammar_string', 'vit_compute',
                'power_mode', 'stop', 'reasoning_format'):
        body.pop(key, None)
    body['reasoning_format'] = 'deepseek'
    limit = body.get('max_completion_tokens', body.get('max_tokens', 384))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError('max_tokens must be a positive integer')
    body.pop('max_tokens', None)
    body['max_completion_tokens'] = min(limit, 512, nctx)
    return body


VOICE_STYLE = (
    "Voice response style: answer in the user's language. Use one short sentence "
    "unless the user explicitly requests more detail. For tool calls, include "
    "schema-required arguments and selectors needed to identify the target "
    "unambiguously. Omit empty optional arguments and never invent values. "
    "Confirm an action only after receiving its tool result."
)


def apply_response_style(body):
    """Prepend a stable instruction without changing the caller's messages."""
    body = dict(body)
    messages = list(body['messages'])
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get('role') == 'system' and isinstance(message.get('content'), str):
            messages[index] = {**message, 'content': VOICE_STYLE + '\n\n' + message['content']}
            break
    else:
        messages.insert(0, {'role': 'system', 'content': VOICE_STYLE})
    body['messages'] = messages
    return body


LOG_MAX_BYTES = 5 * 1024 * 1024


def _log_warning():
    print('WARNING: private log unavailable', file=sys.stderr, flush=True)


def _log_audit_warning():
    print('WARNING: private context audit unavailable', file=sys.stderr, flush=True)


def _log_decision_warning():
    print('WARNING: private decision log unavailable', file=sys.stderr, flush=True)


class _PrivateRotatingHandler(RotatingFileHandler):
    def __init__(self, filename, warn=_log_warning, **kwargs):
        self.warn = warn
        self._secure_paths(os.path.abspath(filename))
        super().__init__(filename, **kwargs)

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                     os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError('Unsafe log file')
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, 'a', encoding='utf-8')
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _secure_paths(base):
        # Validate all retained paths before the stdlib renames/deletes them.
        for path in (base, base + '.1', base + '.2'):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError('Unsafe log file')
            os.chmod(path, 0o600, follow_symlinks=False)

    def doRollover(self):
        self._secure_paths(self.baseFilename)
        super().doRollover()

    def handleError(self, record):
        self.warn()


class JsonlLogger:
    """Bounded JSONL sink for one private directory.

    Directory is a trusted CLI path; do not share it with other processes.
    Failures disable/drop logging, never inference. No root logging handlers.
    """
    filename = ''
    ensure_ascii = True
    warn = staticmethod(_log_warning)

    def __init__(self, directory):
        self.handler = None
        try:
            path = os.path.abspath(directory)
            os.makedirs(path, mode=0o700, exist_ok=True)
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError('Unsafe log directory')
            self.handler = _PrivateRotatingHandler(
                os.path.join(path, self.filename), maxBytes=LOG_MAX_BYTES,
                backupCount=2, warn=self.warn, encoding='utf-8')
            self.handler.setFormatter(logging.Formatter('%(message)s'))
        except Exception:
            self.warn()

    def write(self, record):
        try:
            if self.handler is not None:
                text = json.dumps(record, ensure_ascii=self.ensure_ascii, separators=(',', ':'))
                self.handler.handle(logging.LogRecord('private-response', logging.INFO, '', 0, text, (), None))
        except Exception:
            self.warn()

    def close(self):
        if self.handler is not None:
            self.handler.close()


class ContextAuditLogger(JsonlLogger):
    """Context-format summary and fingerprints, without names, IDs or prompts."""
    filename = 'context-audit.jsonl'
    warn = staticmethod(_log_audit_warning)


class RequestCaptureLogger(JsonlLogger):
    """Opt-in full normalized requests, capped per gateway process."""
    filename = 'request-capture.jsonl'
    ensure_ascii = False  # Keep a full bounded UTF-8 request below the file limit.
    warn = staticmethod(_log_audit_warning)

    def __init__(self, directory, limit=5):
        self.remaining = limit
        self.lock = threading.Lock()
        super().__init__(directory)

    def write(self, record):
        with self.lock:
            if self.remaining <= 0:
                return
            self.remaining -= 1
            super().write(record)


def configured_log_directory(directory, switch):
    """An explicit CLI path opts in unless EnvironmentFile disables the log."""
    value = os.environ.get(switch)
    if value not in (None, '0', '1'):
        raise ValueError(switch + ' must be 0 or 1')
    if value == '0':
        return None
    if value == '1' and not directory:
        raise ValueError(switch + ' requires its log directory argument')
    return directory


class MetricsLogger(JsonlLogger):
    """Payload-free request summaries."""
    filename = 'metrics.jsonl'

    @staticmethod
    def warn():
        print('WARNING: metrics logging unavailable', file=sys.stderr, flush=True)


class RouterMissLogger(JsonlLogger):
    """Explicitly enabled private transcripts, with an optional fixed cutoff."""
    filename = 'router-misses.jsonl'

    @staticmethod
    def warn():
        print('WARNING: private router miss logging unavailable', file=sys.stderr, flush=True)

    def __init__(self, directory, until=None, clock=time.time):
        self.until, self.clock = until, clock
        super().__init__(directory)

    def write(self, record):
        if self.until is None or self.clock() < self.until:
            super().write(record)


def router_miss_logger(directory, until=None):
    """EnvironmentFile settings are read on every gateway start, without setup."""
    enabled = os.environ.get('ROUTER_MISS_LOGGING', '0')
    if enabled not in ('0', '1'):
        raise ValueError('ROUTER_MISS_LOGGING must be 0 or 1')
    # An explicit CLI directory opts in when the environment has no switch.
    if enabled == '0' and 'ROUTER_MISS_LOGGING' in os.environ:
        return None
    if not directory:
        if enabled == '1':
            raise ValueError('Router miss logging requires a log directory')
        return None
    cutoff = until if until is not None else os.environ.get('ROUTER_MISS_UNTIL', '')
    if cutoff:
        parsed = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('ROUTER_MISS_UNTIL requires an explicit timezone')
        cutoff = parsed.timestamp()
    return RouterMissLogger(directory, cutoff if cutoff != '' else None)


def _new_chat_id():
    return 'chatcmpl-' + uuid.uuid4().hex


def build_toolcall_response(model, tool_call):
    """OpenAI chat completion (JSON) carrying the router's tool call proposal."""
    return {
        'id': _new_chat_id(), 'object': 'chat.completion', 'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [{
                    'id': 'call_' + uuid.uuid4().hex[:12], 'type': 'function',
                    'function': {'name': tool_call.name,
                                 'arguments': json.dumps(tool_call.arguments, ensure_ascii=False)},
                }],
            },
            'finish_reason': 'tool_calls',
        }],
    }


def build_clarify_response(model, text):
    return {
        'id': _new_chat_id(), 'object': 'chat.completion', 'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': {'role': 'assistant', 'content': text},
            'finish_reason': 'stop',
        }],
    }


def build_toolcall_sse(tool_call):
    call_id = 'call_' + uuid.uuid4().hex[:12]
    events = [
        {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': None}, 'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{
            'index': 0, 'id': call_id, 'type': 'function',
            'function': {'name': tool_call.name,
                         'arguments': json.dumps(tool_call.arguments, ensure_ascii=False)},
        }]}, 'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
    ]
    return ''.join('data: %s\n\n' % json.dumps(e, ensure_ascii=False) for e in events) + 'data: [DONE]\n\n'


def build_clarify_sse(text):
    events = [
        {'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
    ]
    return ''.join('data: %s\n\n' % json.dumps(e, ensure_ascii=False) for e in events) + 'data: [DONE]\n\n'


def make_handler(upstream, model=MODEL, nctx=4096, api_key='', prompt_counter=None, context_auditor=None, context_audit_fn=None, decision_fn=None, decide_apply=False, clarify_text=None, metrics_logger=None, backend_version='unknown', metrics_profile='default', cache_policy='reuse', response_style='concise', router_miss_logger=None, request_capture=None, agent_language='pl', wait_sound=None):
    if response_style not in ('concise', 'original'):
        raise ValueError('response_style must be concise or original')
    if cache_policy not in ('reuse', 'reset'):
        raise ValueError('cache_policy must be reuse or reset')
    target = urlsplit(upstream)
    if target.scheme != 'http' or not target.hostname or not target.port:
        raise ValueError('upstream must be http://host:port')
    gate = threading.BoundedSemaphore(1)
    run_id = uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'  # EOF frames both SSE and ordinary responses.
        server_version = 'QwenNpuGateway/1'

        def send_response(self, code, message=None):
            if getattr(self, 'metrics', None) is not None:
                self.metrics.record['status'] = code
            super().send_response(code, message)

        def error_json(self, status, message):
            if getattr(self, 'metrics', None) is not None:
                self.metrics.record['outcome'] = 'rejected' if status < 500 else 'backend_error'
            data = json.dumps({'error': {'message': message}}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            if not api_key:
                return True
            supplied = self.headers.get('Authorization', '')
            if hmac.compare_digest(supplied, 'Bearer ' + api_key):
                return True
            self.error_json(401, 'Invalid API key')
            return False

        def send_router_decision(self, decision, body):
            stream = bool(body.get('stream'))
            if decision.kind.value == 'toolcall':
                payload = (build_toolcall_sse(decision.tool_call).encode() if stream
                           else json.dumps(build_toolcall_response(model, decision.tool_call), ensure_ascii=False).encode())
            else:
                if decision.kind.value == 'ack':
                    text = decision.response or 'Wykonano'
                else:
                    text = clarify_text(decision.reason) if clarify_text else 'Nie mogę wykonać tego polecenia.'
                payload = (build_clarify_sse(text).encode() if stream
                           else json.dumps(build_clarify_response(model, text), ensure_ascii=False).encode())
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8' if stream else 'application/json')
            if not stream:
                self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True

        def do_GET(self):
            if not self.allowed():
                return
            if self.path == '/healthz':
                data = b'{"status":"gateway-alive","inference_verified":false}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path in ('/v1/models', '/v1/models/' + model):
                item = {'id': model, 'object': 'model', 'owned_by': 'local'}
                value = {'object': 'list', 'data': [item]} if self.path == '/v1/models' else item
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.error_json(404, 'Not found')

        def do_POST(self):
            self.metrics = RequestMetrics(backend_version, metrics_profile)
            self.metrics.record.update(run_id=run_id, model=model, runtime='llama_cpp',
                                       nctx=nctx, cache_policy=cache_policy, response_style=response_style,
                                       agent_language=agent_language)
            try:
                self._post()
            except (BrokenPipeError, ConnectionResetError):
                self.metrics.record['outcome'] = 'client_disconnected'
            except Exception:
                self.metrics.record['outcome'] = 'error'
                raise
            finally:
                if metrics_logger is not None:
                    self.metrics.finish(metrics_logger)

        def _post(self):
            if not self.allowed():
                return
            if self.path != '/v1/chat/completions':
                self.error_json(404, 'Not found')
                return
            self.connection.settimeout(30)
            began_total = time.monotonic()
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    raise ValueError('Invalid or excessive Content-Length')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('Incomplete request')
                body = normalize_request(json.loads(raw), model, nctx)
                if response_style == 'concise':
                    body = apply_response_style(body)
                self.metrics.record['stream'] = bool(body.get('stream'))
                self.metrics.record['tool_count'] = len(body['tools']) if isinstance(body.get('tools'), list) else 0
                payload = json.dumps(body, ensure_ascii=False).encode()
            except (ValueError, UnicodeError, OSError) as exc:
                self.error_json(400, str(exc))
                return
            # Optional private context audit: format and JSON fingerprints.
            # Computed from the normalized messages and written before the
            # budget gate so every incoming request is summarized. It never
            # touches the response bytes and never records prompt content.
            request_id = self.metrics.record['request_id']
            timestamp = self.metrics.record['timestamp']
            # Capture before routing so local refusals can be diagnosed too.
            # This is opt-in, private and bounded by the per-process sample cap.
            if request_capture is not None:
                capture_started = time.perf_counter()
                try:
                    request_capture.write({'schema_version': 1, 'service': 'request-capture',
                                           'request_id': request_id, 'timestamp': timestamp,
                                           'run_id': run_id, 'body': body})
                except Exception:
                    _log_audit_warning()
                finally:
                    self.metrics.record['capture_ms'] = self.metrics.elapsed(capture_started)
            if context_auditor is not None:
                audit_started = time.perf_counter()
                try:
                    context_auditor.write({'request_id': request_id, 'timestamp': timestamp,
                                           'run_id': run_id, 'model': model,
                                           'agent_language': agent_language,
                                           'cache_policy': cache_policy, 'response_style': response_style,
                                           **context_audit_fn(body)})
                except Exception:
                    _log_audit_warning()
                finally:
                    self.metrics.record['audit_ms'] = self.metrics.elapsed(audit_started)
            # Decision module: compute when applying or collecting missed commands.
            decision = None
            stage = time.perf_counter()
            if decide_apply or router_miss_logger is not None:
                try:
                    decision = decision_fn(body)
                except Exception:
                    _log_decision_warning()
            self.metrics.record['router_ms'] = self.metrics.elapsed(stage)
            if router_miss_logger is not None and decision is not None and decision.kind.value == 'pass':
                try:
                    last = body['messages'][-1] if body['messages'] else {}
                    utterance = last.get('content') if last.get('role') == 'user' else None
                    if isinstance(utterance, list):
                        utterance = ' '.join(part['text'] for part in utterance
                                             if isinstance(part, dict) and part.get('type') == 'text'
                                             and isinstance(part.get('text'), str))
                    if not isinstance(utterance, str):
                        utterance = None
                    if utterance:
                        router_miss_logger.write({
                            'schema_version': 1, 'request_id': request_id, 'timestamp': timestamp,
                            'reason': decision.reason, 'utterance': utterance[:512],
                            'truncated': len(utterance) > 512})
                except Exception:
                    _log_decision_warning()
            # Apply: route recognized HA commands + acks without the LLM.
            if decide_apply and decision is not None and decision.kind.value in ('toolcall', 'clarify', 'ack'):
                self.metrics.record.update(route=decision.kind.value, outcome='completed',
                                           response_kind='toolcall' if decision.kind.value == 'toolcall' else 'text')
                self.send_router_decision(decision, body)
                return
            # The counter must count the FULL rendered template/tools. It fails
            # closed before even the internal reset touches the native SDK.
            # main() always configures one; tests may inject None to isolate the
            # proxy without a tokenizer.
            self.metrics.record['route'] = 'llm'
            if prompt_counter is not None:
                stage = time.perf_counter()
                try:
                    prompt_tokens = prompt_counter(body)
                    if type(prompt_tokens) is not int or prompt_tokens < 0:
                        raise ValueError('Invalid token counter result')
                except Exception as exc:
                    # Only static, known validator categories: never log request data.
                    categories = ('Unsupported request shape', 'Unsupported runtime profile',
                                  'Unsupported tool choice', 'Unsupported parallel tool calls',
                                  'Unsupported tools shape', 'Pinned tools mismatch',
                                  'Unsupported messages shape', 'Unsupported message shape',
                                  'Unsupported message role', 'Unsupported content shape',
                                  'Unsupported tool calls shape', 'Invalid JSON shape')
                    reason = str(exc) if str(exc) in categories else 'counter failure'
                    print('BUDGET_REJECT: ' + reason, flush=True)
                    self.error_json(503, 'Prompt budget verification unavailable; native backend was not contacted')
                    self.metrics.record['outcome'] = 'rejected'
                    return
                finally:
                    self.metrics.record['budget_ms'] = self.metrics.elapsed(stage)
                self.metrics.record['estimated_prompt_tokens'] = prompt_tokens
                required = prompt_tokens + body['max_completion_tokens'] + 64
                if required > nctx:
                    self.error_json(413, 'Context budget exceeded: prompt=%d, output=%d, reserve=64, context=%d; native backend was not contacted' %
                                    (prompt_tokens, body['max_completion_tokens'], nctx))
                    return
            stage = time.perf_counter()
            acquired = gate.acquire(timeout=30)
            self.metrics.record['queue_ms'] = self.metrics.elapsed(stage)
            if not acquired:
                self.error_json(429, 'One inference is already running; retry later')
                return
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=180)
            started = False
            response_status = None
            complete = False
            began = time.monotonic()
            reset_started = None
            backend_started = None
            observer = None
            self.metrics.record['outcome'] = 'backend_error'
            try:
                if cache_policy == 'reset':
                    reset_started = time.perf_counter()
                    reset = normalize_request({'messages': RESET_MESSAGES, 'stream': False,
                                               'max_tokens': 1}, model, nctx)
                    conn.request('POST', '/v1/chat/completions', body=json.dumps(reset).encode(),
                                 headers={'Content-Type': 'application/json'})
                    reset_response = conn.getresponse()
                    reset_body = reset_response.read(MAX_BODY + 1)
                    self.metrics.record['reset_ms'] = self.metrics.elapsed(reset_started)
                    if reset_response.status != 200 or len(reset_body) > MAX_BODY:
                        self.error_json(502, 'Backend cache reset failed')
                        return
                if wait_sound is not None:
                    sound_started = time.perf_counter()
                    try:
                        self.metrics.record['wait_sound_started'] = bool(wait_sound.play())
                    except Exception:
                        self.metrics.record['wait_sound_started'] = False
                        print('WARNING: LLM waiting sound failed; inference is unchanged', file=sys.stderr)
                    finally:
                        self.metrics.record['wait_sound_ms'] = self.metrics.elapsed(sound_started)
                backend_started = time.perf_counter()
                if metrics_logger is not None:
                    observer = ResponseObserver(bool(body.get('stream')))
                conn.request('POST', '/v1/chat/completions', body=payload,
                             headers={'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
                response = conn.getresponse()
                response_status = response.status
                self.send_response(response.status)
                # v0.9.3 tool-call streaming sends data: events with a JSON header.
                content_type = ('text/event-stream; charset=utf-8'
                                if body.get('stream') and response.status == 200
                                else response.getheader('Content-Type', 'application/json'))
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('Connection', 'close')
                self.end_headers()
                started = True
                while True:
                    chunk = response.read1(8192)
                    if not chunk:
                        complete = response.length in (None, 0)
                        break
                    if observer is not None:
                        try:
                            observer.feed(chunk)
                        except Exception:
                            observer._invalidate()
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except OSError:
                        self.metrics.record['outcome'] = 'client_disconnected'
                        raise
                self.metrics.record['outcome'] = ('completed' if complete and response_status == 200 else 'backend_error')
            except (OSError, http.client.HTTPException) as exc:
                # Never append JSON to an already-started SSE stream.
                if not started:
                    self.error_json(502, 'Backend unavailable: ' + type(exc).__name__)
            finally:
                if backend_started is not None:
                    self.metrics.record['backend_ms'] = self.metrics.elapsed(backend_started)
                elif reset_started is not None and self.metrics.record['reset_ms'] is None:
                    self.metrics.record['reset_ms'] = self.metrics.elapsed(reset_started)
                if observer is not None:
                    try:
                        observation = observer.finish()
                        self.metrics.record.update(observation)
                        if self.metrics.record['outcome'] == 'completed':
                            if not observation['observation_complete']:
                                self.metrics.record['outcome'] = 'unverified'
                            elif observation['response_kind'] == 'empty':
                                self.metrics.record['outcome'] = 'empty'
                            elif observation.get('finish_reason') in ('length', 'content_filter'):
                                self.metrics.record['outcome'] = 'incomplete'
                    except Exception:
                        if self.metrics.record['outcome'] == 'completed':
                            self.metrics.record['outcome'] = 'unverified'
                conn.close()
                self.close_connection = True
                gate.release()
        def log_message(self, fmt, *args):
            # Do not log prompts, headers, API keys or tool arguments.
            print('%s %s' % (self.log_date_time_string(), fmt % args), flush=True)

    return Handler


def notify_ready():
    """Signal systemd only after language warmup and binding the HTTP socket."""
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
            notification.sendto(b'READY=1', address)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18191)
    parser.add_argument('--upstream', default='http://127.0.0.1:18192')
    parser.add_argument('--model', default=MODEL)
    parser.add_argument('--nctx', type=int, default=4096)
    parser.add_argument('--agent-language', choices=('pl', 'en'), default=os.environ.get('AGENT_LANGUAGE', 'pl'))
    parser.add_argument('--response-style', choices=('concise', 'original'), default='concise')
    parser.add_argument('--router-miss-log-dir', help='Opt-in private user transcripts for PASS decisions')
    parser.add_argument('--router-miss-until', help='Optional fixed transcript cutoff, overriding ROUTER_MISS_UNTIL')
    parser.add_argument('--cache-policy', choices=('reuse', 'reset'), default='reuse',
                        help='Reuse KV on GenieX 0.11.0, or insert the legacy reset turn')
    parser.add_argument('--metrics-dir', help='Payload-free rotating request metrics (3 x 5 MiB)')
    parser.add_argument('--backend-version', default=os.environ.get('METRICS_BACKEND_VERSION', 'unknown'), help='Operator-supplied runtime version label')
    parser.add_argument('--metrics-profile', default=os.environ.get('METRICS_PROFILE', 'default'), help='Operator-supplied comparison profile')
    parser.add_argument('--prompt-counter-url', help='Loopback CPU prompt-budget service')
    parser.add_argument('--context-audit-dir', help='Private format/fingerprint audit directory, controlled by CONTEXT_AUDIT')
    parser.add_argument('--request-capture-dir', help='Opt-in full normalized requests, controlled by CONTEXT_CAPTURE')
    parser.add_argument('--request-capture-limit', help='Capture at most 1-20 incoming requests per process, overriding CONTEXT_CAPTURE_LIMIT')
    parser.add_argument('--decide-apply', action='store_true', help='Route recognized HA commands via the decision module instead of the LLM (default off)')
    args = parser.parse_args()
    if args.agent_language not in ('pl', 'en'):
        parser.error('AGENT_LANGUAGE must be pl or en')
    if not 2048 <= args.nctx <= 10240:
        parser.error('nctx must be between 2048 and 10240')
    counter = None
    if args.prompt_counter_url:
        import urllib.request
        endpoint = urlsplit(args.prompt_counter_url)
        if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1':
            parser.error('Counter must be loopback HTTP')
        def counter(body):
            request = urllib.request.Request(args.prompt_counter_url,
                data=json.dumps(body, ensure_ascii=False).encode(),
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)['prompt_upper_bound']
    if counter is None:
        parser.error('A prompt counter is required: set GGUF + TOKENIZER or pass --prompt-counter-url')
    try:
        from qwen_wait_sound import from_environment as wait_sound_from_environment
        wait_sound = wait_sound_from_environment()
        miss_logger = router_miss_logger(args.router_miss_log_dir, args.router_miss_until)
        audit_dir = configured_log_directory(args.context_audit_dir, 'CONTEXT_AUDIT')
        capture_dir = configured_log_directory(args.request_capture_dir, 'CONTEXT_CAPTURE')
        capture_limit = int(args.request_capture_limit or os.environ.get('CONTEXT_CAPTURE_LIMIT', '5'))
        if not 1 <= capture_limit <= 20:
            raise ValueError('CONTEXT_CAPTURE_LIMIT must be between 1 and 20')
    except ValueError as exc:
        parser.error(str(exc))
    key = os.environ.get('QWEN_GATEWAY_API_KEY', '')
    metrics_logger = MetricsLogger(args.metrics_dir) if args.metrics_dir else None
    context_auditor = None
    context_audit_fn = None
    if audit_dir:
        from qwen_context_catalog import audit_request
        context_audit_fn = audit_request
        context_auditor = ContextAuditLogger(audit_dir)
    request_capture = RequestCaptureLogger(capture_dir, capture_limit) if capture_dir else None
    decision_fn = None
    clarify_fn = None
    if args.decide_apply or miss_logger is not None:
        from qwen_language import get_language
        from qwen_context_catalog import parse_overview
        from qwen_decision import decide, clarify_text, warmup
        # Validate vocabulary before the best-effort dictionary warmup. Invalid
        # configuration must stop startup, not turn every command into fallback.
        language = get_language(args.agent_language, os.environ.get('ROUTER_VOCABULARY') or None)
        began_warmup = time.perf_counter()
        try:
            warmup(language)
            print(json.dumps({'event': 'router_warmup', 'language': language.code, 'elapsed_ms': round((time.perf_counter()-began_warmup)*1000, 3)}), flush=True)
        except Exception:
            print('WARNING: router language warmup incomplete', file=sys.stderr, flush=True)
        def decision_fn(body):
            catalog = parse_overview(body['messages'])
            return decide(body['messages'], catalog, body.get('tools'),
                          tool_choice=body.get('tool_choice'), language=language)
        clarify_fn = lambda reason: clarify_text(reason, language)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.upstream, args.model, args.nctx, key, prompt_counter=counter, context_auditor=context_auditor, context_audit_fn=context_audit_fn, decision_fn=decision_fn, decide_apply=args.decide_apply, clarify_text=clarify_fn, metrics_logger=metrics_logger, backend_version=args.backend_version, metrics_profile=args.metrics_profile, cache_policy=args.cache_policy, response_style=args.response_style, router_miss_logger=miss_logger, request_capture=request_capture, agent_language=args.agent_language, wait_sound=wait_sound))
    server.daemon_threads = True
    notify_ready()
    print('Qwen gateway listening on %s:%s; authentication=%s' %
          (args.host, args.port, bool(key)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if wait_sound is not None:
            wait_sound.close()
        server.server_close()
        if miss_logger is not None:
            miss_logger.close()
        if metrics_logger is not None:
            metrics_logger.close()
        if context_auditor is not None:
            context_auditor.close()
        if request_capture is not None:
            request_capture.close()


if __name__ == '__main__':
    main()
