#!/usr/bin/env python3
"""Walk through a complete simulated Home Assistant conversation.

The script starts the real gateway handler with a synthetic HA catalog and a
fake GenieX upstream. It then plays HA's side of the loop:

    wake word -> STT -> gateway request -> router or LLM -> tool call
    -> HA executes the tool -> tool result -> gateway ACK -> TTS

No board, model, Docker container or external network is used. Run it on a PC
to see which component handles each turn:

    python tools/simulate_ha_loop.py --list
    python tools/simulate_ha_loop.py router-toolcall --step

Set breakpoints in gateway/chat_gateway.py, gateway/command_router.py and
this file, then use the ``HA loop: full simulated pipeline`` launch config.
"""
from __future__ import annotations

import argparse
from functools import partial
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))

from context_catalog import HEADING  # noqa: E402
from command_router import clarify_text, decide_request  # noqa: E402
from languages import get_language  # noqa: E402
from chat_gateway import MODEL, make_handler  # noqa: E402


HEADING_TEXT = HEADING + '\n'


def catalog_text(rows):
    return HEADING_TEXT + '\n'.join(
        '- names: %s\n  domain: %s%s' % (
            name, domain, '\n  areas: ' + area if area else '')
        for name, domain, area in rows)


def schema(name, *, position=False):
    properties = {
        'name': {'type': 'string'},
        'area': {'type': 'string'},
        'domain': {'type': 'array', 'items': {'type': 'string'}},
    }
    required = ['name', 'domain']
    if position:
        properties['position'] = {'type': 'integer', 'minimum': 0, 'maximum': 100}
        required.append('position')
    return {'type': 'function', 'function': {
        'name': name,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False}}}


def tools():
    return [schema('intent__HassTurnOn'), schema('intent__HassTurnOff'),
            schema('intent__HassSetPosition', position=True)]


def system_message(rows):
    return {
        'role': 'system',
        'content': (
            'You are a Home Assistant voice assistant. '
            'Use the available tools for device actions.\n\n' + catalog_text(rows)),
    }


def user_message(text):
    return {'role': 'user', 'content': text}


def request_body(messages):
    return {
        'model': MODEL,
        'messages': messages,
        'tools': tools(),
        'stream': True,
        'max_tokens': 384,
    }


def sse(events):
    return (''.join('data: %s\n\n' % json.dumps(event, ensure_ascii=False)
                    for event in events) + 'data: [DONE]\n\n').encode()


def text_sse(text):
    return sse([
        {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text},
                      'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
    ])


def tool_sse(name, arguments, call_id='call_simulated_llm'):
    return sse([
        {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': None},
                      'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{
            'index': 0, 'id': call_id, 'type': 'function',
            'function': {'name': name,
                         'arguments': json.dumps(arguments, ensure_ascii=False)},
        }]}, 'finish_reason': None}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
    ])


class FakeUpstream:
    """A tiny GenieX stand-in; only used when the router passes."""

    def __init__(self, mode):
        self.mode = mode
        self.calls = 0
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length) or b'{}')
                with outer.lock:
                    outer.calls += 1
                    call = outer.calls
                messages = body.get('messages') or []
                last = messages[-1] if messages else {}
                last_role = last.get('role') if isinstance(last, dict) else None
                if outer.mode == 'fallback-toolcall' and last_role != 'tool':
                    payload = tool_sse(
                        'intent__HassTurnOn',
                        {'name': 'Światło', 'domain': ['light'], 'area': 'Gabinet'})
                elif outer.mode == 'tool-error' and last_role == 'tool':
                    payload = text_sse('Nie udało się wykonać akcji. Sprawdź urządzenie.')
                else:
                    payload = text_sse('To jest odpowiedź symulowanego modelu.')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Gateway:
    def __init__(self, upstream_url):
        language = get_language('pl')
        handler = make_handler(
            upstream_url, model=MODEL, nctx=10240, prompt_counter=lambda _body: 1000,
            decision_fn=partial(decide_request, language=language), decide_apply=True,
            clarify_text=partial(clarify_text, language=language),
            agent_language='pl', response_style='concise', backend_version='simulation')
        handler.log_message = lambda *_args: None
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Turn:
    def __init__(self, content, tool_calls, finish_reason):
        self.content = content
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason


def post(port, body):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    try:
        conn.request('POST', '/v1/chat/completions',
                     body=json.dumps(body, ensure_ascii=False).encode(),
                     headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        raw = response.read()
        if response.status != 200:
            raise RuntimeError('gateway returned %d: %s' % (
                response.status, raw.decode(errors='replace')[:300]))
        return raw.decode()
    finally:
        conn.close()


def parse_sse(raw):
    content = []
    calls = {}
    finish = None
    for line in raw.splitlines():
        if not line.startswith('data: '):
            continue
        payload = line[6:].strip()
        if payload == '[DONE]':
            break
        event = json.loads(payload)
        choice = (event.get('choices') or [{}])[0]
        delta = choice.get('delta') or {}
        if delta.get('content'):
            content.append(delta['content'])
        for item in delta.get('tool_calls') or []:
            index = item.get('index', 0)
            slot = calls.setdefault(index, {'id': None, 'name': '', 'arguments': ''})
            if item.get('id'):
                slot['id'] = item['id']
            function = item.get('function') or {}
            slot['name'] += function.get('name') or ''
            slot['arguments'] += function.get('arguments') or ''
        if choice.get('finish_reason'):
            finish = choice['finish_reason']
    result = [{'id': slot['id'] or 'call_simulated',
               'name': slot['name'], 'arguments': slot['arguments']}
              for _, slot in sorted(calls.items())]
    return Turn(''.join(content), result, finish)


def execute_tool(call, fail=False):
    function = call['name']
    try:
        args = json.loads(call['arguments'] or '{}')
    except ValueError:
        args = {}
    name = args.get('name') or 'urządzenie'
    if fail:
        return {'error': 'MatchFailedError', 'error_text': 'simulated tool failure'}
    if function == 'intent__HassSetPosition':
        return {'speech': {}, 'response_type': 'action_done', 'data': {
            'success': [{'name': name, 'type': 'entity', 'id': 'simulated.entity'}],
            'failed': []}}
    return {'speech': {}, 'response_type': 'action_done', 'data': {
        'success': [{'name': name, 'type': 'entity', 'id': 'simulated.entity'}],
        'failed': []}}


class Walk:
    def __init__(self, scenario, *, step):
        self.scenario = scenario
        self.step = step
        self.stage_no = 0

    def stage(self, title, detail=''):
        self.stage_no += 1
        print('\n[%d] %s' % (self.stage_no, title), flush=True)
        if detail:
            for line in detail.splitlines():
                print('    ' + line, flush=True)
        if self.step:
            input('    Press Enter to continue...')

    def finish(self, text):
        self.stage('HA -> TTS', 'text: %s\nTTS adapter: simulated (tts-pocket-wyoming)' % repr(text))


SCENARIOS = {
    'router-toolcall': {
        'description': 'simple command -> router tool call -> HA result -> router ACK',
        'user': 'Włącz światło w gabinecie.',
        'rows': [('Światło', 'light', 'Gabinet')],
        'fail_tool': False,
    },
    'router-cover': {
        'description': 'cover position -> router HassSetPosition -> ACK',
        'user': 'Otwórz roletę w sypialni na 50%.',
        'rows': [('Roleta', 'cover', 'Sypialnia')],
        'fail_tool': False,
    },
    'router-clarify': {
        'description': 'ambiguous target -> local CLARIFY -> TTS, no tool',
        'user': 'Włącz światło.',
        'rows': [('Światło', 'light', 'Gabinet'), ('Światło', 'light', 'Salon')],
        'fail_tool': False,
    },
    'fallback-text': {
        'description': 'router passes -> fake LLM text -> TTS',
        'user': 'Jaka jest pogoda?',
        'rows': [('Światło', 'light', 'Gabinet')],
        'fail_tool': False,
    },
    'fallback-toolcall': {
        'description': 'router passes -> fake LLM tool call -> HA result -> router ACK',
        'user': 'Ustaw scenę wieczór.',
        'rows': [('Światło', 'light', 'Gabinet')],
        'fail_tool': False,
    },
    'tool-error': {
        'description': 'router tool call -> HA failure -> LLM explains failure',
        'user': 'Włącz światło w gabinecie.',
        'rows': [('Światło', 'light', 'Gabinet')],
        'fail_tool': True,
    },
}


def run(name, *, step):
    spec = SCENARIOS[name]
    upstream = FakeUpstream(name)
    gateway = Gateway(upstream.url)
    walk = Walk(name, step=step)
    try:
        walk.stage('HA: wake word + audio capture',
                   'LVA: simulated audio / VAD\nSTT: simulated text from scenario')
        walk.stage('HA: STT transcript', spec['user'])
        messages = [system_message(spec['rows']), user_message(spec['user'])]
        walk.stage('HA: build OpenAI request',
                   'system: Static Context with %d row(s)\ntools: %d schemas\n'
                   'prompt counter: 1000 tokens (simulated upper bound)' % (
                       len(spec['rows']), len(tools())))
        before = upstream.calls
        turn = parse_sse(post(gateway.port, request_body(messages)))
        route = 'llm' if upstream.calls > before else 'router'
        where = ('gateway/command_router.py' if route == 'router'
                 else 'gateway/chat_gateway.py -> fake GenieX')
        walk.stage('gateway: first request -> %s' % route,
                   'component: %s\ncontent: %s\ntool calls: %s' % (
                       where,
                       repr(turn.content) if turn.content else '-',
                       json.dumps(turn.tool_calls, ensure_ascii=False) if turn.tool_calls else '-'))
        if not turn.tool_calls:
            walk.finish(turn.content or '(no text)')
            return

        call = turn.tool_calls[0]
        result = execute_tool(call, fail=spec['fail_tool'])
        walk.stage('HA: execute tool', '%s(%s)\nresult: %s' % (
            call['name'], call['arguments'], json.dumps(result, ensure_ascii=False)))
        messages.extend([
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': call['id'], 'type': 'function',
                'function': {'name': call['name'], 'arguments': call['arguments']}}]},
            {'role': 'tool', 'tool_call_id': call['id'],
             'content': json.dumps(result, ensure_ascii=False)},
        ])
        before = upstream.calls
        turn = parse_sse(post(gateway.port, request_body(messages)))
        route = 'llm' if upstream.calls > before else 'router'
        where = ('gateway/command_router.py' if route == 'router'
                 else 'gateway/chat_gateway.py -> fake GenieX')
        walk.stage('gateway: tool-result request -> %s' % route,
                   'component: %s\ncontent: %s\ntool calls: %s' % (
                       where,
                       repr(turn.content) if turn.content else '-',
                       json.dumps(turn.tool_calls, ensure_ascii=False) if turn.tool_calls else '-'))
        walk.finish(turn.content or '(no text)')
    finally:
        gateway.close()
        upstream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', nargs='?', default='router-toolcall',
                        choices=sorted(SCENARIOS))
    parser.add_argument('--list', action='store_true', help='list scenarios and exit')
    parser.add_argument('--step', action='store_true',
                        help='pause after every stage and wait for Enter')
    args = parser.parse_args()
    if args.list:
        for name in sorted(SCENARIOS):
            print('%-20s %s' % (name, SCENARIOS[name]['description']))
        return
    run(args.scenario, step=args.step)


if __name__ == '__main__':
    main()
