#!/usr/bin/env python3
"""Pinned GenieX v0.9.3 gateway; stdlib only, never executes HA tools.

Only model catalog and chat completions are exposed. SSE is passed through
without parsing or buffering the full response. LAN listener is unauthenticated
unless QWEN_GATEWAY_API_KEY is set. Do not expose it to the public Internet.
"""
import argparse
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import stat
import sys
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
import time
import uuid
import threading
from urllib.parse import urlsplit

MODEL = 'bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0'
MAX_BODY = 2 * 1024 * 1024
# v0.9.3 returns an empty stop/zero tokens when the exact prompt is repeated.
# A tiny internal, tool-free turn forces its session-key reset without unloading
# weights or modifying the real HA prompt. Its output is discarded, never acted on.
RESET_MESSAGES = [{'role': 'system', 'content': 'Internal cache reset. Reply OK.'},
                  {'role': 'user', 'content': '.'}]


def normalize_request(body, model=MODEL, nctx=4096, runtime='llama_cpp'):
    if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
        raise ValueError('messages must be an array')
    body = dict(body)
    # No caller may redirect compute, enable thinking or enlarge the context.
    if runtime not in ('llama_cpp', 'qairt'):
        raise ValueError('Unsupported runtime profile')
    # QAIRT context length is compiled into the bundle; positive nctx/ngl
    # are llama.cpp-only knobs and are rejected by its plugin.
    body.update(model=model, compute='npu' if runtime == 'qairt' else 'HTP0',
                ngl=0 if runtime == 'qairt' else -1,
                nctx=0 if runtime == 'qairt' else nctx,
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


RESPONSE_CAPTURE_LIMIT = 64 * 1024
RESPONSE_LOG_MAX_BYTES = 5 * 1024 * 1024


def _log_warning():
    print('WARNING: private response logging unavailable', file=sys.stderr, flush=True)


def _log_audit_warning():
    print('WARNING: private context audit unavailable', file=sys.stderr, flush=True)


def _log_shadow_warning():
    print('WARNING: private decision shadow unavailable', file=sys.stderr, flush=True)


class _PrivateRotatingHandler(RotatingFileHandler):
    def __init__(self, filename, **kwargs):
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
        _log_warning()


class ResponseLogger:
    """Opt-in, response-only JSONL sink. One instance per private directory.

    Directory is a trusted CLI path; do not share it with other processes.
    Failures disable/drop logging, never inference. No root logging handlers.
    """
    filename = 'responses.jsonl'
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
                os.path.join(path, self.filename), maxBytes=RESPONSE_LOG_MAX_BYTES,
                backupCount=2, encoding='utf-8')
            self.handler.setFormatter(logging.Formatter('%(message)s'))
        except Exception:
            self.warn()

    def write(self, record):
        try:
            if self.handler is not None:
                text = json.dumps(record, ensure_ascii=True, separators=(',', ':'))
                self.handler.handle(logging.LogRecord('private-response', logging.INFO, '', 0, text, (), None))
        except Exception:
            self.warn()

    def close(self):
        if self.handler is not None:
            self.handler.close()


class DecisionShadowLogger(ResponseLogger):
    """Metadata-only decision log. Payload filtering belongs to sanitize()."""
    filename = 'decision-shadow.jsonl'
    warn = staticmethod(_log_shadow_warning)


class ContextAuditLogger(ResponseLogger):
    """Context-format summary, without names, IDs or prompts."""
    filename = 'context-audit.jsonl'
    warn = staticmethod(_log_audit_warning)


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


def make_handler(upstream, model=MODEL, nctx=4096, api_key='', runtime='llama_cpp', prompt_counter=None, response_logger=None, context_auditor=None, context_audit_fn=None, decision_shadow=None, decision_fn=None, decide_apply=False, clarify_text=None, sanitize_fn=None):
    target = urlsplit(upstream)
    if target.scheme != 'http' or not target.hostname or not target.port:
        raise ValueError('upstream must be http://host:port')
    gate = threading.BoundedSemaphore(1)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'  # EOF frames both SSE and ordinary responses.
        server_version = 'QwenNpuGateway/1'

        def error_json(self, status, message):
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
                body = normalize_request(json.loads(raw), model, nctx, runtime)
                payload = json.dumps(body, ensure_ascii=False).encode()
            except (ValueError, UnicodeError, OSError) as exc:
                self.error_json(400, str(exc))
                return
            # Optional private context audit: presence/format summary only.
            # Computed from the normalized messages and written before the
            # budget gate so every incoming request is summarized. It never
            # touches the response bytes and never records prompt content.
            request_id = None
            timestamp = None
            if context_auditor is not None or response_logger is not None or decision_shadow is not None:
                request_id = uuid.uuid4().hex
                timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            if context_auditor is not None:
                try:
                    context_auditor.write({'request_id': request_id, 'timestamp': timestamp,
                                           **context_audit_fn(body['messages'])})
                except Exception:
                    _log_audit_warning()
            # Decision module: compute now (for shadow and/or apply).
            decision = None
            if decision_shadow is not None or decide_apply:
                try:
                    decision = decision_fn(body)
                except Exception:
                    _log_shadow_warning()
            # Apply: route recognized HA commands + acks without the LLM.
            if decide_apply and decision is not None and decision.kind.value in ('toolcall', 'clarify', 'ack'):
                self.send_router_decision(decision, body)
                if decision_shadow is not None:
                    try:
                        decision_shadow.write({
                            'request_id': request_id, 'timestamp': timestamp,
                            'total_ms': round((time.monotonic() - began_total) * 1000),
                            'inference_ms': 0,
                            'status': 200, 'stream': bool(body.get('stream')),
                            **sanitize_fn(decision)})
                    except Exception:
                        _log_shadow_warning()
                return
            # A configured counter must count the FULL rendered template/tools.
            # Fail closed before even the internal reset touches the native SDK.
            # Legacy profiles without a counter retain their previous behaviour;
            # guarded deployments must explicitly configure a verified counter.
            if prompt_counter is not None:
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
                    return
                required = prompt_tokens + body['max_completion_tokens'] + 64
                if required > nctx:
                    self.error_json(413, 'Context budget exceeded: prompt=%d, output=%d, reserve=64, context=%d; native backend was not contacted' %
                                    (prompt_tokens, body['max_completion_tokens'], nctx))
                    return
            if not gate.acquire(timeout=30):
                self.error_json(429, 'One inference is already running; retry later')
                return
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=180)
            started = False
            capture = bytearray() if response_logger is not None else None
            response_status = None
            complete = False
            truncated = False
            began = time.monotonic()
            try:
                reset = normalize_request({'messages': RESET_MESSAGES, 'stream': False,
                                           'max_tokens': 1}, model, nctx, runtime)
                conn.request('POST', '/v1/chat/completions', body=json.dumps(reset).encode(),
                             headers={'Content-Type': 'application/json'})
                reset_response = conn.getresponse()
                reset_body = reset_response.read(MAX_BODY + 1)
                if reset_response.status != 200 or len(reset_body) > MAX_BODY:
                    self.error_json(502, 'Backend cache reset failed')
                    return
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
                    if capture is not None:
                        remaining = RESPONSE_CAPTURE_LIMIT - len(capture)
                        capture.extend(chunk[:remaining])
                        truncated = truncated or len(chunk) > remaining
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException) as exc:
                # Never append JSON to an already-started SSE stream.
                if not started:
                    self.error_json(502, 'Backend unavailable: ' + type(exc).__name__)
            finally:
                conn.close()
                self.close_connection = True
                gate.release()
                total_ms = round((time.monotonic() - began_total) * 1000)
                inference_ms = round((time.monotonic() - began) * 1000)
                if capture is not None and response_status is not None:
                    try:
                        response_logger.write({
                            'request_id': request_id, 'timestamp': timestamp,
                            'status': response_status, 'stream': bool(body.get('stream')),
                            'truncated': truncated, 'complete': complete,
                            'elapsed_ms': inference_ms, 'total_ms': total_ms,
                            'response': capture.decode('utf-8', errors='replace')})
                    except Exception:
                        _log_warning()
                if decision_shadow is not None and decision is not None:
                    try:
                        decision_shadow.write({
                            'request_id': request_id, 'timestamp': timestamp,
                            'total_ms': total_ms, 'inference_ms': inference_ms,
                            'status': response_status, 'stream': bool(body.get('stream')),
                            **sanitize_fn(decision)})
                    except Exception:
                        _log_shadow_warning()

        def log_message(self, fmt, *args):
            # Do not log prompts, headers, API keys or tool arguments.
            print('%s %s' % (self.log_date_time_string(), fmt % args), flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18191)
    parser.add_argument('--upstream', default='http://127.0.0.1:18192')
    parser.add_argument('--model', default=MODEL)
    parser.add_argument('--nctx', type=int, default=4096)
    parser.add_argument('--runtime', choices=('llama_cpp', 'qairt'), default='llama_cpp')
    parser.add_argument('--prompt-counter-url', help='Loopback CPU prompt-budget service')
    parser.add_argument('--response-log-dir', help='Opt-in private response/tool-argument JSONL directory (0700; 3 x 5 MiB; captures 64 KiB per response)')
    parser.add_argument('--context-audit-dir', help='Opt-in private HA context-audit JSONL directory (0700; presence/format summary only; never stores prompts)')
    parser.add_argument('--decide-shadow-dir', help='Opt-in private decision-module shadow JSONL directory (0700; observes routing, never changes responses)')
    parser.add_argument('--decide-apply', action='store_true', help='Route recognized HA commands via the decision module instead of the LLM (default off)')
    args = parser.parse_args()
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
    if args.nctx > 4096 and counter is None:
        parser.error('Large-context deployment requires an explicit budget guard')
    key = os.environ.get('QWEN_GATEWAY_API_KEY', '')
    response_logger = ResponseLogger(args.response_log_dir) if args.response_log_dir else None
    context_auditor = None
    context_audit_fn = None
    if args.context_audit_dir:
        from qwen_context_catalog import audit_context
        context_audit_fn = audit_context
        context_auditor = ContextAuditLogger(args.context_audit_dir)
    decision_shadow = None
    decision_fn = None
    sanitize_fn = None
    clarify_fn = None
    if args.decide_shadow_dir or args.decide_apply:
        from qwen_context_catalog import parse_overview
        from qwen_decision import decide, sanitize, clarify_text
        def decision_fn(body):
            catalog = parse_overview(body['messages'])
            return decide(body['messages'], catalog, body.get('tools'),
                          tool_choice=body.get('tool_choice'))
        sanitize_fn = sanitize
        clarify_fn = clarify_text
        if args.decide_shadow_dir:
            decision_shadow = DecisionShadowLogger(args.decide_shadow_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.upstream, args.model, args.nctx, key, args.runtime, prompt_counter=counter, response_logger=response_logger, context_auditor=context_auditor, context_audit_fn=context_audit_fn, decision_shadow=decision_shadow, decision_fn=decision_fn, decide_apply=args.decide_apply, clarify_text=clarify_fn, sanitize_fn=sanitize_fn))
    server.daemon_threads = True
    print('Qwen gateway listening on %s:%s; authentication=%s' %
          (args.host, args.port, bool(key)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if response_logger is not None:
            response_logger.close()
        if context_auditor is not None:
            context_auditor.close()
        if decision_shadow is not None:
            decision_shadow.close()


if __name__ == '__main__':
    main()
